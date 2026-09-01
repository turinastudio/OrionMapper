from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Mapping
from typing import Any

import httpx

from orion_mapper.core.config import Settings
from orion_mapper.core.config import settings as global_settings
from orion_mapper.core.rate_limiter import TokenBucketLimiter

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.RemoteProtocolError,
)


class HttpClientError(Exception):
    """Base exception for HTTP client operations."""


class HttpTimeoutError(HttpClientError):
    """Raised when an HTTP operation exceeds configured timeout."""


class HttpRateLimitError(HttpClientError):
    """Raised when an HTTP request is rate limited repeatedly."""


class MaxRetriesExceededError(HttpClientError):
    """Raised when max retry attempts are exhausted."""


class AsyncHttpClient:
    """
    Production-grade resilient Async HTTP client with connection pooling,
    User-Agent rotation, rate limiting, and jittered exponential backoff.
    """

    def __init__(
        self,
        config: Settings | None = None,
        rate_limiter: TokenBucketLimiter | None = None,
        custom_headers: Mapping[str, str] | None = None,
    ) -> None:
        self.config = config or global_settings
        self.rate_limiter = rate_limiter
        self.custom_headers = dict(custom_headers) if custom_headers else {}

        limits = httpx.Limits(
            max_connections=self.config.http_pool_max_connections,
            max_keepalive_connections=self.config.http_pool_max_keepalive,
            keepalive_expiry=30.0,
        )
        timeout = httpx.Timeout(
            timeout=self.config.http_timeout,
            connect=10.0,
            read=self.config.http_timeout,
            write=10.0,
            pool=10.0,
        )

        http2_enabled = False
        try:
            import h2  # noqa: F401
            http2_enabled = True
        except ImportError:
            pass

        self._client = httpx.AsyncClient(
            limits=limits,
            timeout=timeout,
            follow_redirects=True,
            http2=http2_enabled,
        )

    def _get_random_headers(self) -> dict[str, str]:
        ua = (
            random.choice(self.config.user_agents)
            if self.config.user_agents
            else "OrionMapper/1.0"
        )
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
            "Accept-Language": "es-ES,es;q=0.9,en-US;q=0.8,en;q=0.7",
            "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
            "Sec-Ch-Ua-Mobile": "?0",
            "Sec-Ch-Ua-Platform": '"Linux"',
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
            "Sec-Fetch-User": "?1",
            "Upgrade-Insecure-Requests": "1",
        }
        headers.update(self.custom_headers)
        return headers

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Any | None = None,
        json: Any | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        rate_limiter: TokenBucketLimiter | None = None,
    ) -> httpx.Response:
        limiter = rate_limiter or self.rate_limiter
        req_timeout = timeout or self.config.http_timeout
        max_retries = self.config.http_max_retries

        for attempt in range(max_retries + 1):
            if limiter:
                await limiter.acquire()

            req_headers = self._get_random_headers()
            if headers:
                req_headers.update(headers)

            try:
                response = await self._client.request(
                    method=method,
                    url=url,
                    params=params,
                    data=data,
                    json=json,
                    headers=req_headers,
                    timeout=req_timeout,
                )

                if response.status_code in RETRYABLE_STATUS_CODES:
                    if attempt == max_retries:
                        raise MaxRetriesExceededError(
                            f"HTTP {response.status_code} for {method} {url} after {max_retries + 1} attempts"
                        )

                    retry_after = response.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        backoff = float(retry_after)
                    else:
                        jitter = random.uniform(0.1, 0.5)
                        backoff = min(
                            self.config.http_backoff_max,
                            self.config.http_backoff_factor * (2 ** attempt) + jitter,
                        )
                    logger.warning(
                        "HTTP %s for %s %s (attempt %d/%d). Retrying in %.2fs",
                        response.status_code,
                        method,
                        url,
                        attempt + 1,
                        max_retries + 1,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue

                response.raise_for_status()
                return response

            except RETRYABLE_EXCEPTIONS as exc:
                if attempt == max_retries:
                    raise MaxRetriesExceededError(
                        f"Failed {method} {url} after {max_retries + 1} attempts: {exc}"
                    ) from exc

                jitter = random.uniform(0.1, 0.5)
                backoff = min(
                    self.config.http_backoff_max,
                    self.config.http_backoff_factor * (2 ** attempt) + jitter,
                )
                logger.warning(
                    "Network error %s for %s %s (attempt %d/%d). Retrying in %.2fs",
                    type(exc).__name__,
                    method,
                    url,
                    attempt + 1,
                    max_retries + 1,
                    backoff,
                )
                await asyncio.sleep(backoff)

        raise MaxRetriesExceededError(f"Exhausted retries for {method} {url}")

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        rate_limiter: TokenBucketLimiter | None = None,
    ) -> httpx.Response:
        return await self.request(
            "GET",
            url,
            params=params,
            headers=headers,
            timeout=timeout,
            rate_limiter=rate_limiter,
        )

    async def post(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        data: Any | None = None,
        json: Any | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
        rate_limiter: TokenBucketLimiter | None = None,
    ) -> httpx.Response:
        return await self.request(
            "POST",
            url,
            params=params,
            data=data,
            json=json,
            headers=headers,
            timeout=timeout,
            rate_limiter=rate_limiter,
        )

    async def get_text(self, url: str, **kwargs: Any) -> str:
        res = await self.get(url, **kwargs)
        return res.text

    async def get_json(self, url: str, **kwargs: Any) -> Any:
        res = await self.get(url, **kwargs)
        return res.json()

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> AsyncHttpClient:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()
