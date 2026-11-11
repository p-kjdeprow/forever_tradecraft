"""The in-process advice cache: reuse, expiry, eviction and one computation per key."""

from __future__ import annotations

import threading
import time

import pytest

from crowley.advisor.cache import AdviceCache


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_hit_miss_and_expiry():
    clock = Clock()
    cache: AdviceCache[int] = AdviceCache(ttl_seconds=10, clock=clock)
    calls = []

    def compute() -> int:
        calls.append(1)
        return len(calls)

    assert cache.get_or_compute("k", compute) == 1
    assert cache.get_or_compute("k", compute) == 1
    clock.now = 11  # past the ttl: recomputed
    assert cache.get_or_compute("k", compute) == 2
    assert cache.get_or_compute("other", compute) == 3


def test_least_recently_used_entry_is_evicted():
    cache: AdviceCache[str] = AdviceCache(max_entries=2)
    cache.get_or_compute("a", lambda: "a")
    cache.get_or_compute("b", lambda: "b")
    cache.get_or_compute("a", lambda: "x")  # touch a
    cache.get_or_compute("c", lambda: "c")  # evicts b
    assert len(cache) == 2
    assert cache.get_or_compute("a", lambda: "recomputed") == "a"
    assert cache.get_or_compute("b", lambda: "recomputed") == "recomputed"


def test_concurrent_requests_for_one_key_compute_once():
    cache: AdviceCache[int] = AdviceCache()
    started = threading.Event()
    calls = []

    def slow() -> int:
        calls.append(1)
        started.set()
        time.sleep(0.2)
        return 42

    results: list[int] = []
    threads = [
        threading.Thread(target=lambda: results.append(cache.get_or_compute("k", slow))) for _ in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results == [42] * 4 and len(calls) == 1


def test_a_failed_computation_is_not_cached():
    cache: AdviceCache[int] = AdviceCache()

    def boom() -> int:
        raise RuntimeError("no")

    with pytest.raises(RuntimeError):
        cache.get_or_compute("k", boom)
    assert len(cache) == 0
    assert cache.get_or_compute("k", lambda: 5) == 5


def test_invalid_sizes_fail_loudly():
    with pytest.raises(ValueError):
        AdviceCache(max_entries=0)
    with pytest.raises(ValueError):
        AdviceCache(ttl_seconds=0)


def test_failed_computation_leaves_no_lock_behind():
    cache: AdviceCache[int] = AdviceCache()

    def boom() -> int:
        raise RuntimeError("no")

    for _ in range(3):
        with pytest.raises(RuntimeError):
            cache.get_or_compute("k", boom)
    assert cache.lock_count == 0
    cache.get_or_compute("k", lambda: 1)
    assert cache.lock_count == 1  # a successful key keeps its lock while cached
    cache.clear()
    assert cache.lock_count == 0


@pytest.mark.parametrize(("slots", "peak"), [(1, 1), (2, 2)])
def test_distinct_keys_compute_at_most_slots_at_a_time(slots, peak):
    cache: AdviceCache[int] = AdviceCache(max_entries=8, max_concurrent_computes=slots)
    running = 0
    highest = 0
    guard = threading.Lock()

    def compute() -> int:
        nonlocal running, highest
        with guard:
            running += 1
            highest = max(highest, running)
        time.sleep(0.15)
        with guard:
            running -= 1
        return 1

    threads = [threading.Thread(target=cache.get_or_compute, args=(f"k{i}", compute)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert highest == peak


def test_a_zero_compute_slots_cache_is_rejected():
    with pytest.raises(ValueError):
        AdviceCache(max_concurrent_computes=0)
