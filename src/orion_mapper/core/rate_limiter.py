from __future__ import annotations

import asyncio
import time
from typing import Any, ClassVar


class TokenBucketLimiter:
    """
    Async token bucket rate limiter supporting burst limits and smooth throttling.
    """

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        if rate <= 0:
            raise ValueError(f"Rate must be > 0, got {rate}")
        self.rate = float(rate)
        self.capacity = float(capacity if capacity is not None and capacity > 0 else rate)
        self.tokens = self.capacity
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> float:
        """
        Acquire given number of tokens. Blocks asynchronously until tokens are available.
        Returns total seconds waited.
        """
        if tokens > self.capacity:
            raise ValueError(
                f"Requested tokens ({tokens}) exceeds bucket capacity ({self.capacity})"
            )

        total_waited = 0.0
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_refill = now

            if self.tokens < tokens:
                needed = tokens - self.tokens
                wait_time = needed / self.rate
                total_waited += wait_time
                await asyncio.sleep(wait_time)

                # Update refill state after sleep
                now_after = time.monotonic()
                elapsed_after = now_after - self.last_refill
                self.tokens = min(self.capacity, self.tokens + elapsed_after * self.rate)
                self.last_refill = now_after

            self.tokens -= tokens

        return total_waited

    async def __aenter__(self) -> TokenBucketLimiter:
        await self.acquire(1.0)
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        pass


class RateLimiterRegistry:
    """Registry managing domain/provider-specific rate limiters."""

    _instances: ClassVar[dict[str, TokenBucketLimiter]] = {}
    _lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    @classmethod
    def get_limiter(
        cls,
        name: str,
        rate: float = 5.0,
        capacity: float | None = None,
    ) -> TokenBucketLimiter:
        key = name.strip().lower()
        if key not in cls._instances:
            cls._instances[key] = TokenBucketLimiter(rate=rate, capacity=capacity)
        return cls._instances[key]

    @classmethod
    def reset(cls) -> None:
        cls._instances.clear()
