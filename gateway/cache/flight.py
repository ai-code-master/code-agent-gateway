"""Single-flight request coalescing."""

import hashlib
import json
import threading


class SingleFlight:
    def __init__(self, enabled=lambda: True, timeout=lambda: 30):
        self._enabled = enabled
        self._timeout = timeout
        self._lock = threading.Lock()
        self._inflight = {}

    def _make_key(self, method: str, path: str, body: bytes) -> str:
        if not self._enabled():
            return ""
        if method != "POST" or path not in (
            "/v1/chat/completions", "/chat/completions"
        ):
            return ""
        try:
            if json.loads(body).get("stream"):
                return ""
        except (AttributeError, json.JSONDecodeError):
            return ""
        return hashlib.sha256(f"{method}:{path}:".encode() + body).hexdigest()

    def do(self, method: str, path: str, body: bytes, callable_fn):
        key = self._make_key(method, path, body)
        if not key:
            return callable_fn(), False
        with self._lock:
            entry = self._inflight.get(key)
            if entry is None:
                entry = {"event": threading.Event(), "result": None, "error": None}
                self._inflight[key] = entry
                is_leader = True
            else:
                is_leader = False
        if not is_leader:
            if not entry["event"].wait(timeout=self._timeout()):
                return callable_fn(), False
            if entry["error"]:
                raise entry["error"]
            return entry["result"], True
        try:
            entry["result"] = callable_fn()
            return entry["result"], False
        except Exception as error:
            entry["error"] = error
            raise
        finally:
            entry["event"].set()
            with self._lock:
                if self._inflight.get(key) is entry:
                    del self._inflight[key]
