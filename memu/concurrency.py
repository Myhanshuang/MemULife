"""Serialize state changes while allowing bounded model work to overlap."""
from contextlib import contextmanager
import threading


class WriteLock:
    def __init__(self):
        self.lock = threading.RLock()
        self.local = threading.local()

    def __enter__(self):
        self.lock.acquire()
        self.local.depth = getattr(self.local, 'depth', 0) + 1
        return self

    def __exit__(self, *_):
        self.local.depth -= 1
        self.lock.release()

    @contextmanager
    def model_work(self):
        # Legacy full-index rebuilds pin their snapshot. Other blocking model or
        # index work releases all nested write-lock levels.
        depth = 0 if getattr(self.local, 'pinned', 0) else getattr(self.local, 'depth', 0)
        for _ in range(depth):
            self.__exit__()
        try:
            yield
        finally:
            for _ in range(depth):
                self.__enter__()

    @contextmanager
    def pinned(self):
        with self:
            self.local.pinned = getattr(self.local, 'pinned', 0) + 1
            try:
                yield
            finally:
                self.local.pinned -= 1
