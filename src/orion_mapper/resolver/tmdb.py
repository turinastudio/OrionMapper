from __future__ import annotations

import logging
from typing import Any, Literal

import httpx

from orion_mapper.core.config import settings
from orion_mapper.core.http import AsyncHttpClient
from orion_mapper.core.rate_limiter import RateLimiterRegistry, TokenBucketLimiter

logger = logging.getLogger(__name__)


class TmdbClient:
    """Production-grade async client for TMDB v3 API with rate limiting and retry handling."""

    def __init__(
        self,
        api_key: str | None = None,
        rate_limiter: TokenBucketLimiter | None = None,
        http_client: AsyncHttpClient | httpx.AsyncClient | None = None,
        base_url: str = "https://api.themoviedb.org",
    ) -> None:
        self.api_key = (
            api_key
            if (api_key is not None and len(api_key.strip()) > 0)
            else (settings.tmdb_api_key or "34fafb223263c2461f8f88a3489cb92e")
        )
        self.rate_limiter = rate_limiter or RateLimiterRegistry.get_limiter(
            "tmdb",
            rate=settings.tmdb_rate_limit,
            capacity=settings.tmdb_rate_burst,
        )
        self.base_url = base_url.rstrip("/")
        self._owns_http_client = False

        if http_client is not None:
            self.http_client = http_client
        else:
            self.http_client = AsyncHttpClient(rate_limiter=self.rate_limiter)
            self._owns_http_client = True

    async def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        url = f"{self.base_url}{path}" if path.startswith("/") else f"{self.base_url}/{path}"
        query_params: dict[str, Any] = {"api_key": self.api_key}
        if params:
            query_params.update({k: v for k, v in params.items() if v is not None})

        if self.rate_limiter and not isinstance(self.http_client, AsyncHttpClient):
            await self.rate_limiter.acquire()

        try:
            if isinstance(self.http_client, AsyncHttpClient):
                response = await self.http_client.request(
                    method=method,
                    url=url,
                    params=query_params,
                    rate_limiter=self.rate_limiter,
                )
            else:
                response = await self.http_client.request(
                    method=method,
                    url=url,
                    params=query_params,
                )

            if response.status_code == 404:
                return None
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            logger.warning("TMDB HTTP error for %s: %s", url, exc)
            return None
        except Exception as exc:
            logger.warning("TMDB request failed for %s: %s", url, exc)
            return None

    async def find_by_imdb_id(self, imdb_id: str) -> dict[str, Any] | None:
        """Resolve TMDB entity from IMDb ID via `/3/find/{imdb_id}?external_source=imdb_id`."""
        if not imdb_id:
            return None
        cleaned = imdb_id.strip().lower()
        if not cleaned:
            return None

        data = await self._request("GET", f"/3/find/{cleaned}", {"external_source": "imdb_id"})
        if not data or not isinstance(data, dict):
            return None

        movie_results = data.get("movie_results") or []
        if movie_results and isinstance(movie_results, list) and len(movie_results) > 0:
            res = dict(movie_results[0])
            res.setdefault("media_type", "movie")
            return res

        tv_results = data.get("tv_results") or []
        if tv_results and isinstance(tv_results, list) and len(tv_results) > 0:
            res = dict(tv_results[0])
            res.setdefault("media_type", "tv")
            return res

        return None

    async def get_external_ids(
        self,
        tmdb_id: str | int,
        media_type: Literal["movie", "tv", "series"] | str,
    ) -> dict[str, Any] | None:
        """Fetch external identifiers (IMDb ID, TVDB ID) via `/3/{type}/{id}/external_ids`."""
        if not tmdb_id:
            return None
        clean_id = str(tmdb_id).strip()
        if not clean_id:
            return None

        norm_type = "tv" if str(media_type).lower() in ("tv", "series") else "movie"
        return await self._request("GET", f"/3/{norm_type}/{clean_id}/external_ids")

    async def search(
        self,
        title: str,
        media_type: Literal["movie", "tv", "series"] | str,
        year: int | None = None,
        language: str = "es-MX",
    ) -> list[dict[str, Any]]:
        """Search TMDB candidates by title via `/3/search/{type}`."""
        if not title or not title.strip():
            return []

        norm_type = "tv" if str(media_type).lower() in ("tv", "series") else "movie"
        params: dict[str, Any] = {"query": title.strip(), "language": language}
        if year is not None:
            if norm_type == "movie":
                params["year"] = str(year)
            else:
                params["first_air_date_year"] = str(year)

        data = await self._request("GET", f"/3/search/{norm_type}", params)
        if not data or "results" not in data or not isinstance(data["results"], list):
            return []
        return data["results"]

    async def get_details(
        self,
        tmdb_id: str | int,
        media_type: Literal["movie", "tv", "series"] | str,
        language: str = "es-419",
    ) -> dict[str, Any] | None:
        """Fetch full media details with appended external IDs, images, and videos."""
        if not tmdb_id:
            return None
        clean_id = str(tmdb_id).strip()
        if not clean_id:
            return None

        norm_type = "tv" if str(media_type).lower() in ("tv", "series") else "movie"
        append = (
            "images,release_dates,videos"
            if norm_type == "movie"
            else "external_ids,images,content_ratings,videos"
        )
        return await self._request(
            "GET",
            f"/3/{norm_type}/{clean_id}",
            {"language": language, "append_to_response": append},
        )

    async def close(self) -> None:
        if self._owns_http_client and isinstance(self.http_client, AsyncHttpClient):
            await self.http_client.close()

    async def __aenter__(self) -> TmdbClient:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()
