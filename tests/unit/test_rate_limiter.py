import asyncio
import time

import pytest

from orion_mapper.core.rate_limiter import (
    RateLimiterRegistry,
    TokenBucketLimiter,
)


@pytest.mark.asyncio
async def test_rate_limiter_invalid_parameters():
    with pytest.raises(ValueError):
        TokenBucketLimiter(rate=0.0)

    with pytest.raises(ValueError):
        TokenBucketLimiter(rate=-5.0)

    limiter = TokenBucketLimiter(rate=5.0, capacity=2.0)
    with pytest.raises(ValueError):
        await limiter.acquire(3.0)


@pytest.mark.asyncio
async def test_rate_limiter_burst():
    # 10 tokens capacity at 2 tokens/sec
    limiter = TokenBucketLimiter(rate=2.0, capacity=10.0)

    start = time.monotonic()
    # Acquire 5 tokens immediately from initial burst
    for _ in range(5):
        await limiter.acquire(1.0)
    elapsed = time.monotonic() - start

    # Initial burst should be virtually instantaneous (< 100ms)
    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_rate_limiter_throttling():
    # 2 tokens capacity at 10 tokens/sec
    limiter = TokenBucketLimiter(rate=10.0, capacity=2.0)

    # Consume all initial capacity
    await limiter.acquire(2.0)

    start = time.monotonic()
    # Next 2 tokens require waiting at 10 req/s (approx 0.2s)
    await limiter.acquire(2.0)
    elapsed = time.monotonic() - start

    assert elapsed >= 0.15


@pytest.mark.asyncio
async def test_rate_limiter_concurrent_tasks():
    limiter = TokenBucketLimiter(rate=30.0, capacity=5.0)

    async def worker():
        async with limiter:
            return True

    # 10 concurrent tasks
    results = await asyncio.gather(*(worker() for _ in range(10)))
    assert all(results)
    assert len(results) == 10


def test_rate_limiter_registry():
    l1 = RateLimiterRegistry.get_limiter("gnula", rate=5.0)
    l2 = RateLimiterRegistry.get_limiter("GNULA", rate=10.0)
    assert l1 is l2
    assert l1.rate == 5.0

    l3 = RateLimiterRegistry.get_limiter("serieskao", rate=2.0)
    assert l3 is not l1
    assert l3.rate == 2.0
