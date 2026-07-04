import time
from collections import defaultdict, deque


class RateLimiter:
    def __init__(self, limit=12, window_seconds=10, now_func=None):
        self.limit = limit
        self.window_seconds = window_seconds
        self.now_func = now_func or time.time
        self._hits = defaultdict(deque)

    def allow(self, key):
        now = float(self.now_func())
        bucket = self._hits[key]
        cutoff = now - self.window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self.limit:
            return False
        bucket.append(now)
        return True
