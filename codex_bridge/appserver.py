"""Persistent pool for the locally authenticated Codex App Server."""

import os
import queue
import threading
import time

from .process import AppServerProcess
from .protocol import AppServerError, await_turn as _await_turn


class AppServerPool:
    def __init__(self, size):
        self.size = max(1, size)
        self._available = queue.LifoQueue()
        self._all = []
        self._lock = threading.Lock()
        self._wait_count = 0
        self._wait_total = 0.0
        self._timeout_count = 0

    def run(self, *args, pool_timeout=None, **kwargs):
        process = self._acquire(
            pool_timeout if pool_timeout is not None else kwargs.get("timeout", 900)
        )
        healthy = True
        try:
            return process.run(*args, **kwargs)
        except Exception:
            healthy = False
            raise
        finally:
            self._release(process, healthy)

    def probe(self):
        with self._lock:
            if any(process.alive for process in self._all):
                return True
        process = self._acquire(10)
        try:
            process.start()
            return process.alive
        finally:
            self._release(process, process.alive)

    def models(self, timeout=8):
        process = self._acquire(timeout)
        healthy = True
        try:
            return process.models(timeout)
        except Exception:
            healthy = False
            raise
        finally:
            self._release(process, healthy)

    def stats(self):
        with self._lock:
            return {
                "size": self.size,
                "processes": len(self._all),
                "idle": self._available.qsize(),
                "healthy": sum(item.alive for item in self._all),
                "busy": len(self._all) - self._available.qsize(),
                "wait_count": self._wait_count,
                "avg_wait_ms": round(
                    self._wait_total * 1000 / self._wait_count, 2
                ) if self._wait_count else 0.0,
                "timeout_count": self._timeout_count,
            }

    def close(self):
        with self._lock:
            processes, self._all = self._all, []
            self._available = queue.LifoQueue()
        for process in processes:
            process.close()

    def _acquire(self, timeout):
        try:
            process = self._available.get_nowait()
        except queue.Empty:
            with self._lock:
                if len(self._all) < self.size:
                    process = AppServerProcess()
                    self._all.append(process)
                    return process
            try:
                started = time.monotonic()
                process = self._available.get(timeout=timeout)
            except queue.Empty as error:
                with self._lock:
                    self._timeout_count += 1
                raise AppServerError("Codex process pool is busy") from error
            with self._lock:
                self._wait_count += 1
                self._wait_total += time.monotonic() - started
        if process.alive or process.proc is None:
            return process
        self._discard(process)
        return self._acquire(timeout)

    def _release(self, process, healthy):
        if healthy and (process.alive or process.proc is None):
            self._available.put(process)
        else:
            self._discard(process)

    def _discard(self, process):
        process.close()
        with self._lock:
            if process in self._all:
                self._all.remove(process)


_POOL = None
_POOL_LOCK = threading.Lock()


def _pool():
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            raw = os.environ.get("CAG_CODEX_POOL_SIZE", "2")
            _POOL = AppServerPool(int(raw))
        return _POOL


def run_codex(prompt, cwd, tools=None, effort=None, model=None,
              timeout=900, pool_timeout=None, on_delta=None):
    return _pool().run(
        prompt, cwd, tools=tools, effort=effort, model=model,
        timeout=timeout, pool_timeout=pool_timeout, on_delta=on_delta,
    )


def probe_codex():
    try:
        return _pool().probe()
    except Exception:
        return False


def discover_models(timeout=8):
    return _pool().models(timeout)


def pool_stats():
    return _pool().stats()


def shutdown_pool():
    if _POOL is not None:
        _POOL.close()
