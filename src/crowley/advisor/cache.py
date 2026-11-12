"""An in-process cache of computed advice.

Advice for a (house, build, latest sweep, character, options) is deterministic, and computing it
takes tens of seconds of CPU, so the server computes it once per key and serves the result.  The
key includes the latest sweep id, so a new sweep naturally misses; ``ttl_seconds`` (one sweep
cadence by default) additionally bounds entries whose inputs can change without a new sweep: a
character capture is read at request time, not at the sweep, so it can arrive after an entry was
computed and is picked up when the entry expires.

A miss computes under a per-key lock: concurrent requests for one key wait for one computation
instead of each running their own.  A semaphore (``max_concurrent_computes``, default 1) also bounds
how many different keys compute at once: each computation is tens of seconds of CPU on a request
thread, and the route's threadpool and database pool are shared with scan ingest.  The cache is per
process; the server runs one.  A failed computation is not cached and leaves no lock behind.

Warming: ``service.warm`` computes the default advice into this cache. The scan upload route calls
it after each newly ingested sweep, making the first request after that sweep a cache hit.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable


class AdviceCache[T]:
    def __init__(
        self,
        max_entries: int = 8,
        ttl_seconds: float = 900.0,
        clock: Callable[[], float] = time.monotonic,
        max_concurrent_computes: int = 1,
    ) -> None:
        if max_entries < 1 or ttl_seconds <= 0 or max_concurrent_computes < 1:
            raise ValueError("a cache needs at least one entry, a positive ttl and one compute slot")
        self._slots = threading.BoundedSemaphore(max_concurrent_computes)
        self._max = max_entries
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[Hashable, tuple[float, T]] = OrderedDict()
        self._locks: dict[Hashable, threading.Lock] = {}
        self._guard = threading.Lock()

    def _fresh(self, key: Hashable) -> T | None:
        entry = self._entries.get(key)
        if entry is None or self._clock() - entry[0] > self._ttl:
            return None
        self._entries.move_to_end(key)
        return entry[1]

    def get_or_compute(self, key: Hashable, compute: Callable[[], T]) -> T:
        with self._guard:
            hit = self._fresh(key)
            if hit is not None:
                return hit
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            with self._guard:
                hit = self._fresh(key)
            if hit is not None:
                return hit
            try:
                with self._slots:
                    value = compute()  # no cache lock and no database connection is held here
            except BaseException:
                with self._guard:
                    if self._locks.get(key) is lock:
                        del self._locks[key]
                raise
            with self._guard:
                self._entries[key] = (self._clock(), value)
                self._entries.move_to_end(key)
                while len(self._entries) > self._max:
                    evicted, _ = self._entries.popitem(last=False)
                    self._locks.pop(evicted, None)
            return value

    def clear(self) -> None:
        with self._guard:
            self._entries.clear()
            self._locks.clear()

    @property
    def lock_count(self) -> int:
        return len(self._locks)

    def __len__(self) -> int:
        return len(self._entries)
