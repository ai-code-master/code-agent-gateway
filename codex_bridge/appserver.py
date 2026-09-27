"""Persistent pool for the locally authenticated Codex App Server."""

import os
import queue
import threading

from .process import AppServerProcess
from .protocol import AppServerError, await_turn as _await_turn


class AppServerPool:
    def __init__(self, size):
        self.size = max(1, size)
        self._available = queue.LifoQueue()
        self._all = []
        self._lock = threading.Lock()

    def run(self, *args, **kwargs):
        process = self._acquire(kwargs.get("timeout", 900))
        healthy = True
        try:
            return process.run(*args, **kwargs)
        except Exception:
            healthy = False
            raise
        finally:
            self._release(process, healthy)

    def probe(self):
        process = self._acquire(10)
        try:
            process.start()
            return process.alive
        finally:
            self._release(process, process.alive)

    def stats(self):
        with self._lock:
            return {
                "size": self.size,
                "processes": len(self._all),
                "idle": self._available.qsize(),
                "healthy": sum(item.alive for item in self._all),
            }

    def close(self):
        with self._lock:
            processes, self._all = self._all, []
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
                process = self._available.get(timeout=timeout)
            except queue.Empty as error:
                raise AppServerError("Codex process pool is busy") from error
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
              timeout=900, on_delta=None):
    return _pool().run(
        prompt, cwd, tools=tools, effort=effort, model=model,
        timeout=timeout, on_delta=on_delta,
    )


def probe_codex():
    try:
        return _pool().probe()
    except Exception:
        return False


def pool_stats():
    return _pool().stats()


def shutdown_pool():
    if _POOL is not None:
        _POOL.close()
