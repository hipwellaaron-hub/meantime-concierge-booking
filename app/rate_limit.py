"""Minimal in-memory rate limiting for public write endpoints.

Deliberately not a distributed limiter (no Redis) -- this matches the
"near-zero running cost" target and the reality of a single Railway
instance. If this service is ever scaled to multiple instances, each
would track its own counters independently, which would make the
effective limit `max_requests * instance_count`. Fine for blunting basic
scripted abuse against a single small venue's public forms; not a
substitute for a real WAF/rate-limiting layer if abuse becomes a genuine
problem.
"""

import threading
import time
from collections import deque

from fastapi import HTTPException, Request


class InMemoryRateLimiter:
    def __init__(self, max_requests: int, window_seconds: float):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._hits: dict[str, deque] = {}
        # FastAPI runs sync routes in a thread pool, so check() can
        # genuinely be called concurrently by different requests -- the
        # check-then-append sequence below isn't atomic on its own, and
        # without this lock two requests racing at exactly the threshold
        # could both be admitted.
        self._lock = threading.Lock()

    def check(self, key: str) -> bool:
        """Returns True if the request is allowed, False if rate-limited.

        Note: self._hits gains one entry per distinct IP ever seen and
        entries are never swept once a key goes idle -- a real but, at
        this app's traffic volume (a single small venue's public forms),
        negligible bound on memory (a handful of KB even over a year of
        uptime). A periodic sweep would be needed if that stopped being
        true; not worth the added complexity here.
        """
        now = time.monotonic()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] > self.window_seconds:
                hits.popleft()
            if len(hits) >= self.max_requests:
                return False
            hits.append(now)
            return True


def client_ip(request: Request) -> str:
    # Same precedence as app/api/documents.py's _client_ip: Railway (and
    # most PaaS) sits behind a proxy, so request.client.host alone would
    # just be the proxy's address.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit_dependency(limiter: InMemoryRateLimiter):
    def _dependency(request: Request):
        if not limiter.check(client_ip(request)):
            raise HTTPException(status_code=429, detail="Too many requests -- please try again shortly")

    return _dependency
