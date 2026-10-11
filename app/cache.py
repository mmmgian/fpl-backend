"""In-process async TTL cache with single-flight loading.

- Concurrent callers for the same key share one in-flight load (no thundering herd on FPL).
- Failures are never cached: the next caller retries.
- If the caller that owns a load is cancelled, waiters start their own load instead of failing.
- Bounded: least-recently-used entries are evicted beyond `max_entries`.
Runs on a single event loop, so plain dict operations between awaits need no locking.
"""
from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Hashable


def _self_cancelling() -> bool:
    task = asyncio.current_task()
    cancelling = getattr(task, "cancelling", None)        # Python 3.11+
    return bool(cancelling and cancelling())


class AsyncTTLCache:
    def __init__(self, max_entries: int = 512, clock: Callable[[], float] = time.monotonic):
        self._values: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._inflight: dict[Hashable, asyncio.Future] = {}
        self._max = max_entries
        self._clock = clock

    def _fresh(self, key: Hashable) -> tuple[bool, Any]:
        hit = self._values.get(key)
        if hit is not None and hit[0] > self._clock():
            self._values.move_to_end(key)
            return True, hit[1]
        return False, None

    async def get_or_load(self, key: Hashable, ttl: float, load: Callable[[], Awaitable[Any]]) -> Any:
        while True:
            ok, value = self._fresh(key)
            if ok:
                return value
            pending = self._inflight.get(key)
            if pending is None:
                return await self._load(key, ttl, load)
            try:
                return await asyncio.shield(pending)
            except asyncio.CancelledError:
                if pending.cancelled() and not _self_cancelling():
                    continue            # the owner was cancelled, not us: load it ourselves
                raise

    async def _load(self, key: Hashable, ttl: float, load: Callable[[], Awaitable[Any]]) -> Any:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[key] = fut
        try:
            value = await load()
        except asyncio.CancelledError:
            fut.cancel()
            raise
        except Exception as e:
            fut.set_exception(e)
            fut.exception()          # mark retrieved: no "exception never retrieved" noise without waiters
            raise
        finally:
            if self._inflight.get(key) is fut:
                del self._inflight[key]
        fut.set_result(value)
        if ttl > 0:
            self._values[key] = (self._clock() + ttl, value)
            self._values.move_to_end(key)
            while len(self._values) > self._max:
                self._values.popitem(last=False)
        return value

    def clear(self) -> None:
        self._values.clear()
