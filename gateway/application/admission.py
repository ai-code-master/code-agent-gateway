"""Bounded request admission shared by every provider."""

import threading
import time


class RequestLease:
    def __init__(self, runtime, semaphore):
        self._runtime = runtime
        self._semaphore = semaphore

    def __enter__(self):
        return self._runtime.begin_request()

    def __exit__(self, _type, _value, _traceback):
        self._runtime.end_request()
        self._semaphore.release()


class AdmissionController:
    def __init__(self, runtime, limit):
        self._runtime = runtime
        self._lock = threading.Lock()
        self._limit = limit
        self._semaphore = threading.BoundedSemaphore(limit)
        self._closing = threading.Event()

    def acquire(self, timeout):
        started = time.monotonic()
        with self._lock:
            semaphore = self._semaphore
        deadline = started + timeout
        acquired = False
        while not self._closing.is_set() and time.monotonic() < deadline:
            if semaphore.acquire(timeout=min(0.25, max(0, deadline - time.monotonic()))):
                acquired = True
                break
        wait = time.monotonic() - started
        lease = RequestLease(self._runtime, semaphore) if acquired else None
        return lease, wait

    def close(self):
        self._closing.set()

    def configure(self, limit):
        with self._lock:
            if limit == self._limit:
                return
            self._limit = limit
            self._semaphore = threading.BoundedSemaphore(limit)

    @property
    def limit(self):
        with self._lock:
            return self._limit
