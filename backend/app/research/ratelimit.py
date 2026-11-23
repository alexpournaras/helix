"""Adaptive per-provider concurrency (PLAN B5.1 step 8): halve on 429/529, honour Retry-After,
add one after a run of successes (AIMD)."""

from __future__ import annotations

import asyncio
import time


class AdaptiveLimiter:
    def __init__(self, maximum: int, *, success_run: int = 10, clock=time.monotonic, sleep=asyncio.sleep):
        self.maximum = max(1, maximum)
        self.limit = self.maximum
        self.active = 0
        self.success_run = success_run
        self._successes = 0
        self._paused_until = 0.0
        self._cond = asyncio.Condition()
        self._clock = clock
        self._sleep = sleep

    async def acquire(self) -> None:
        while True:
            wait = self._paused_until - self._clock()
            if wait > 0:
                await self._sleep(wait)
                continue
            async with self._cond:
                if self.active < self.limit and self._paused_until <= self._clock():
                    self.active += 1
                    return
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass

    async def release(self) -> None:
        async with self._cond:
            self.active = max(0, self.active - 1)
            self._cond.notify_all()

    def on_success(self) -> None:
        self._successes += 1
        if self._successes >= self.success_run and self.limit < self.maximum:
            self.limit += 1
            self._successes = 0

    def on_throttle(self, retry_after: float | None = None) -> None:
        self.limit = max(1, self.limit // 2)
        self._successes = 0
        if retry_after:
            self._paused_until = max(self._paused_until, self._clock() + min(retry_after, 600))

    async def __aenter__(self) -> "AdaptiveLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.release()


class Limiters:
    """One limiter per provider, sized from `concurrency` settings."""

    def __init__(self, concurrency: dict[str, int]):
        self.concurrency = concurrency
        self._limiters: dict[str, AdaptiveLimiter] = {}

    def get(self, provider: str) -> AdaptiveLimiter:
        if provider not in self._limiters:
            self._limiters[provider] = AdaptiveLimiter(int(self.concurrency.get(provider, 4)))
        return self._limiters[provider]
