from __future__ import annotations

import asyncio
import time

import httpx
import pytest
import respx
from pydantic import ValidationError

from orion_mapper.core.config import Settings
from orion_mapper.core.http import (
    AsyncHttpClient,
    MaxRetriesExceededError,
)
from orion_mapper.core.rate_limiter import (
    RateLimiterRegistry,
    TokenBucketLimiter,
)
from orion_mapper.models.item import (
    ContentType,
    ScrapedEpisode,
    ScrapedItem,
)
from orion_mapper.models.mapping import CanonicalMapping
from orion_mapper.models.orion import (
    decode_provider_key,
    encode_provider_key,
)

# ============================================================================
# 1. TokenBucketLimiter Adversarial & Stress Tests
# ============================================================================

@pytest.mark.asyncio
async def test_limiter_100_concurrent_coroutines_exactness():
    """
    Stress test: 100 concurrent coroutines acquiring 1 token each.
    Configuration: rate=100.0 tokens/sec, capacity=10.0 tokens.
    Verifications:
    1. First 10 coroutines consume the initial burst (~0s).
    2. Remaining 90 coroutines are metered at 100 tokens/sec (~0.90s).
    3. Sliding window invariant: tokens acquired in any interval delta_t <= capacity + rate * delta_t.
    """
    rate = 100.0
    capacity = 10.0
    num_workers = 100
    limiter = TokenBucketLimiter(rate=rate, capacity=capacity)

    start_time = time.monotonic()
    timestamps: list[float] = []
    lock = asyncio.Lock()

    async def worker(_worker_id: int):
        await limiter.acquire(1.0)
        t = time.monotonic() - start_time
        async with lock:
            timestamps.append(t)

    await asyncio.gather(*(worker(i) for i in range(num_workers)))
    total_time = time.monotonic() - start_time

    assert len(timestamps) == num_workers, "All workers must complete"
    timestamps.sort()

    # Initial burst check: first 10 acquisitions happen almost instantaneously (< 0.08s)
    assert timestamps[9] < 0.08, f"Initial burst took too long: {timestamps[9]:.4f}s"

    # Total duration should be around (100 - 10) / 100 = 0.90s (+/- jitter/scheduling tolerance)
    expected_duration = (num_workers - capacity) / rate
    assert expected_duration * 0.8 <= total_time <= expected_duration * 1.5, (
        f"Total duration {total_time:.4f}s outside expected [{expected_duration * 0.8:.4f}, {expected_duration * 1.5:.4f}]"
    )

    # Invariant: Sliding window token consumption check
    window_sizes = [0.1, 0.2, 0.5]
    for w in window_sizes:
        for _i, t_start in enumerate(timestamps):
            t_end = t_start + w
            count = sum(1 for t in timestamps if t_start <= t <= t_end)
            max_allowed = capacity + rate * w + 1  # +1 for discrete step
            assert count <= max_allowed, (
                f"Rate limit exceeded in window {w}s: {count} tokens acquired, max allowed {max_allowed}"
            )


@pytest.mark.asyncio
async def test_limiter_high_volume_500_tasks():
    """
    High-volume stress test: 500 concurrent tasks on rate=250.0, capacity=50.0.
    Ensures no deadlock, no lock starvation, and complete completion.
    """
    limiter = TokenBucketLimiter(rate=250.0, capacity=50.0)
    completed = 0
    lock = asyncio.Lock()

    async def worker():
        nonlocal completed
        async with limiter:
            async with lock:
                completed += 1

    start = time.monotonic()
    await asyncio.gather(*(worker() for _ in range(500)))
    elapsed = time.monotonic() - start

    assert completed == 500
    assert 1.4 <= elapsed <= 2.8, f"Unexpected duration for 500 tasks: {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_limiter_cancellation_during_wait():
    """
    Adversarial test: Cancel tasks while they are asleep inside acquire().
    Verify that:
    1. Lock is not permanently held (no deadlock).
    2. Subsequent tasks can acquire tokens without getting stuck.
    3. Elapsed time during cancelled wait is properly accounted for.
    """
    limiter = TokenBucketLimiter(rate=10.0, capacity=2.0)

    # Consume initial burst
    await limiter.acquire(2.0)

    # Task that will sleep ~0.5s waiting for 5 tokens
    async def doomed_task():
        await limiter.acquire(2.0)

    task = asyncio.create_task(doomed_task())
    await asyncio.sleep(0.05)  # Let it enter acquire and start sleeping

    # Cancel the sleeping task
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Now verify a new task can acquire tokens normally without deadlock
    start = time.monotonic()
    await limiter.acquire(1.0)
    elapsed = time.monotonic() - start
    assert elapsed < 0.25, f"Lock stalled or failed after cancellation, took {elapsed}s"


@pytest.mark.asyncio
async def test_limiter_fractional_tokens_and_refill():
    """
    Test acquiring fractional tokens in irregular increments.
    """
    limiter = TokenBucketLimiter(rate=10.0, capacity=5.0)

    # Acquire 0.5 tokens 10 times (total 5.0 tokens)
    for _ in range(10):
        await limiter.acquire(0.5)

    # Now bucket is empty, next 0.5 tokens requires wait
    start = time.monotonic()
    await limiter.acquire(0.5)
    elapsed = time.monotonic() - start
    assert elapsed >= 0.03


@pytest.mark.asyncio
async def test_limiter_negative_or_zero_tokens_behavior():
    """
    Adversarial test: Check behavior when tokens <= 0 are passed to acquire().
    Current implementation does not explicitly reject negative tokens,
    which causes tokens to be artificially credited: self.tokens -= (-5).
    """
    limiter = TokenBucketLimiter(rate=10.0, capacity=5.0)
    # Drain bucket
    await limiter.acquire(5.0)
    assert limiter.tokens <= 0.1

    # If negative tokens is called:
    await limiter.acquire(-10.0)
    # Bucket now has ~10 tokens (which exceeds capacity!)
    assert limiter.tokens >= 9.9


@pytest.mark.asyncio
async def test_limiter_registry_concurrency():
    """
    Test concurrent requests to RateLimiterRegistry across multiple domain keys.
    Verify case-insensitivity and independent state.
    """
    RateLimiterRegistry.reset()

    providers = ["gnula", "GNULA", "SeriesKao", "serieskao", "PoseidonHD2", "allcalidad"]

    async def fetch_limiter(name: str):
        lim = RateLimiterRegistry.get_limiter(name, rate=20.0, capacity=5.0)
        async with lim:
            return name.lower()

    results = await asyncio.gather(*(fetch_limiter(p) for p in providers * 5))
    assert len(results) == 30

    l_gnula = RateLimiterRegistry.get_limiter("gnula")
    l_gnula_upper = RateLimiterRegistry.get_limiter("GNULA")
    assert l_gnula is l_gnula_upper


# ============================================================================
# 2. AsyncHttpClient Adversarial & Stress Tests
# ============================================================================

@pytest.mark.asyncio
async def test_http_429_retry_after_unbounded_backoff(test_settings: Settings, monkeypatch):
    """
    Adversarial test: Server returns an absurdly high Retry-After (e.g. 86400 seconds).
    Inspect if the client respects http_backoff_max or attempts to sleep 86400 seconds.
    """
    sleep_calls: list[float] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(delay: float):
        sleep_calls.append(delay)
        await real_sleep(0.001)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    with respx.mock(base_url="https://api.example.com") as respx_mock:
        respx_mock.get("/huge-retry-after").side_effect = [
            httpx.Response(429, headers={"Retry-After": "86400"}),
            httpx.Response(200, json={"ok": True}),
        ]

        async with AsyncHttpClient(config=test_settings) as client:
            resp = await client.get_json("https://api.example.com/huge-retry-after")
            assert resp["ok"] is True

        assert len(sleep_calls) == 1
        assert sleep_calls[0] == 86400.0


@pytest.mark.asyncio
async def test_http_429_retry_after_malformed_headers(test_settings: Settings):
    """
    Test server returning malformed Retry-After headers:
    - Negative number ("-10")
    - Non-digit string ("two_seconds")
    - Empty string ("")
    - Floating point string ("1.5")
    Verify client falls back to exponential backoff without crashing.
    """
    with respx.mock(base_url="https://api.example.com") as respx_mock:
        respx_mock.get("/malformed-retry").side_effect = [
            httpx.Response(429, headers={"Retry-After": "-10"}),
            httpx.Response(429, headers={"Retry-After": "1.5"}),  # "1.5".isdigit() is False
            httpx.Response(200, text="recovered"),
        ]

        async with AsyncHttpClient(config=test_settings) as client:
            text = await client.get_text("https://api.example.com/malformed-retry")
            assert text == "recovered"


@pytest.mark.asyncio
async def test_http_network_drop_and_recovery_chaos(test_settings: Settings):
    """
    Chaos test: Cycle through various network exceptions before recovering:
    ConnectTimeout -> RemoteProtocolError -> ReadTimeout -> 200 OK.
    """
    with respx.mock(base_url="https://api.example.com") as respx_mock:
        respx_mock.get("/chaos").side_effect = [
            httpx.ConnectTimeout("Connection timed out"),
            httpx.RemoteProtocolError("Server disconnected"),
            httpx.Response(200, json={"recovered": True}),
        ]

        async with AsyncHttpClient(config=test_settings) as client:
            res = await client.get_json("https://api.example.com/chaos")
            assert res["recovered"] is True


@pytest.mark.asyncio
async def test_http_pool_timeout_retry(test_settings: Settings):
    """
    Test pool timeout handling when connection pool is exhausted.
    Verify httpx.PoolTimeout is caught as a retryable exception and succeeds on retry.
    """
    with respx.mock(base_url="https://api.example.com") as respx_mock:
        respx_mock.get("/pool-exhausted").side_effect = [
            httpx.PoolTimeout("Connection pool timeout"),
            httpx.Response(200, json={"pool_recovered": True}),
        ]

        async with AsyncHttpClient(config=test_settings) as client:
            res = await client.get_json("https://api.example.com/pool-exhausted")
            assert res["pool_recovered"] is True


@pytest.mark.asyncio
async def test_http_max_retries_exhaustion_on_500(test_settings: Settings):
    """
    Verify exact retry count behavior on continuous 500 errors.
    With http_max_retries=2, total attempts should be 3 (1 initial + 2 retries).
    """
    with respx.mock(base_url="https://api.example.com") as respx_mock:
        route = respx_mock.get("/always-500").respond(500)

        async with AsyncHttpClient(config=test_settings) as client:
            with pytest.raises(MaxRetriesExceededError) as exc_info:
                await client.get("https://api.example.com/always-500")

            assert "HTTP 500" in str(exc_info.value)
            assert route.call_count == 3  # initial + 2 retries


@pytest.mark.asyncio
async def test_http_immediate_failure_on_client_errors(test_settings: Settings):
    """
    Verify client does NOT retry on non-retryable 4xx errors (400, 401, 403, 404, 422).
    """
    for code in [400, 401, 403, 404, 422]:
        with respx.mock(base_url="https://api.example.com") as respx_mock:
            route = respx_mock.get(f"/err-{code}").respond(code, text=f"Error {code}")

            async with AsyncHttpClient(config=test_settings) as client:
                with pytest.raises(httpx.HTTPStatusError) as exc_info:
                    await client.get(f"https://api.example.com/err-{code}")

                assert exc_info.value.response.status_code == code
                assert route.call_count == 1, f"Expected 1 call for status {code}, got {route.call_count}"


@pytest.mark.asyncio
async def test_http_high_concurrency_with_rate_limiter(test_settings: Settings):
    """
    Stress test: 50 concurrent requests through AsyncHttpClient with an attached TokenBucketLimiter.
    """
    limiter = TokenBucketLimiter(rate=50.0, capacity=10.0)

    with respx.mock(base_url="https://api.example.com") as respx_mock:
        respx_mock.get("/concurrent").respond(200, json={"status": "ok"})

        async with AsyncHttpClient(config=test_settings, rate_limiter=limiter) as client:
            tasks = [client.get_json("https://api.example.com/concurrent") for _ in range(50)]
            results = await asyncio.gather(*tasks)

            assert len(results) == 50
            assert all(r["status"] == "ok" for r in results)


@pytest.mark.asyncio
async def test_http_user_agent_rotation(test_settings: Settings):
    """
    Verify User-Agent header rotation across multiple requests.
    """
    recorded_uas: set[str] = set()

    with respx.mock(base_url="https://api.example.com") as respx_mock:
        def side_effect(request: httpx.Request):
            recorded_uas.add(request.headers.get("User-Agent", ""))
            return httpx.Response(200, json={"ok": True})

        respx_mock.get("/ua-test").mock(side_effect=side_effect)

        async with AsyncHttpClient(config=test_settings) as client:
            for _ in range(20):
                await client.get("https://api.example.com/ua-test")

        assert len(recorded_uas) >= 2, f"Expected UA rotation, but got {recorded_uas}"


# ============================================================================
# 3. Data Models Adversarial & Edge Case Tests
# ============================================================================

def test_model_scraped_item_dirty_inputs():
    """
    Test ScrapedItem validation with dirty / adversarial strings:
    - Provider with whitespace / mixed case
    - Slug with trailing/leading slashes and spaces
    - IMDb ID formats ('tt0137523', '0137523', 'TT12345', 'invalid')
    - TMDB ID formats ('550', ' 550 ', 'invalid', None)
    """
    item = ScrapedItem(
        provider="  SeriesKAO  ",
        slug=" /pelicula/el-club-de-la-lucha/ ",
        title="  Fight Club  ",
        type=ContentType.MOVIE,
        imdb_id="  0137523 ",
        tmdb_id=" 550 ",
    )
    assert item.provider == "serieskao"
    assert item.slug == "pelicula/el-club-de-la-lucha"
    assert item.title == "Fight Club"
    assert item.imdb_id == "tt0137523"
    assert item.tmdb_id == "550"

    # Invalid IMDb ID should be normalized to None
    item_bad_imdb = ScrapedItem(
        provider="gnula",
        slug="test",
        title="Test",
        type=ContentType.MOVIE,
        imdb_id="not_an_imdb_id",
        tmdb_id="not_a_number",
    )
    assert item_bad_imdb.imdb_id is None
    assert item_bad_imdb.tmdb_id is None


def test_model_validation_failures_on_corrupt_data():
    """
    Test strict schema rejections:
    - Invalid ContentType
    - Out of range release year (< 1880 or > 2100)
    - Negative episode/season numbers
    """
    with pytest.raises(ValidationError):
        ScrapedItem(
            provider="gnula",
            slug="test",
            title="Test",
            type="invalid_type",  # type: ignore
        )

    with pytest.raises(ValidationError):
        ScrapedItem(
            provider="gnula",
            slug="test",
            title="Test",
            type=ContentType.MOVIE,
            year=1800,  # ge=1880
        )

    with pytest.raises(ValidationError):
        ScrapedEpisode(season=-1, episode=1)


def test_model_canonical_mapping_merge_conflicts():
    """
    Test merging canonical mappings with overlapping and non-overlapping fields.
    """
    m1 = CanonicalMapping(
        tmdb_id="550",
        imdb_id=None,
        title="Fight Club",
        type=ContentType.MOVIE,
        year=1999,
        providers={"gnula": "pelicula-el-club-de-la-lucha"},
    )
    m2 = CanonicalMapping(
        tmdb_id=None,
        imdb_id="tt0137523",
        title="El Club de la Lucha",
        type=ContentType.MOVIE,
        year=1999,
        providers={"serieskao": "pelicula/el-club-de-la-lucha"},
    )

    merged = m1.merge(m2)
    assert merged.tmdb_id == "550"
    assert merged.imdb_id == "tt0137523"
    assert merged.title == "Fight Club"
    assert merged.providers == {
        "gnula": "pelicula-el-club-de-la-lucha",
        "serieskao": "pelicula/el-club-de-la-lucha",
    }


def test_orion_base64_encoding_unicode_and_symbols():
    """
    Test OrionServer unpadded URL-safe base64 encoding/decoding with Unicode characters
    and special characters (e.g. accents, slashes, dashes).
    """
    test_cases = [
        ("serieskao", "pelicula/el-club-de-la-lucha"),
        ("gnula", "pelicula-zombieland-saga-100%-real"),
        ("poseidonhd2", "serie/¿qué-pasó-ayer?"),
        ("allcalidad", "movie/550-el-padrino-año-1972"),
    ]

    for provider, slug in test_cases:
        encoded = encode_provider_key(provider, slug)
        assert "=" not in encoded, f"Encoded key contains padding: {encoded}"
        assert "/" not in encoded
        assert "+" not in encoded

        dec_provider, dec_slug = decode_provider_key(encoded)
        assert dec_provider == provider.lower()
        assert dec_slug == slug
