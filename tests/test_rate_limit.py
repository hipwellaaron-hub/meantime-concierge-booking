import threading

from app.rate_limit import InMemoryRateLimiter


def test_allows_up_to_the_limit():
    limiter = InMemoryRateLimiter(max_requests=3, window_seconds=60)
    assert [limiter.check("k") for _ in range(3)] == [True, True, True]
    assert limiter.check("k") is False


def test_different_keys_are_independent():
    limiter = InMemoryRateLimiter(max_requests=1, window_seconds=60)
    assert limiter.check("a") is True
    assert limiter.check("b") is True
    assert limiter.check("a") is False


def test_concurrent_checks_never_admit_more_than_the_limit():
    """FastAPI runs sync routes in a thread pool, so check() can genuinely
    be called concurrently for the same key. Without the lock, the
    check-then-append sequence isn't atomic and a race could let more
    than max_requests through."""
    limiter = InMemoryRateLimiter(max_requests=5, window_seconds=60)
    barrier = threading.Barrier(20)
    results = []
    results_lock = threading.Lock()

    def attempt():
        barrier.wait(timeout=5)
        allowed = limiter.check("same-key")
        with results_lock:
            results.append(allowed)

    threads = [threading.Thread(target=attempt) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count(True) == 5
    assert results.count(False) == 15
