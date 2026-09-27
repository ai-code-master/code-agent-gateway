"""Exact and conservative normalized-response cache."""

import hashlib
import json
import threading
import time


class ResponseCache:
    def __init__(self, ttl=300, max_entries=100, enabled=lambda: True,
                 semantic_enabled=lambda: True):
        self._ttl = ttl
        self._max_entries = max_entries
        self._enabled = enabled
        self._semantic_enabled = semantic_enabled
        self._lock = threading.Lock()
        self._cache = {}
        self._signatures = {}
        self._hit_count = self._miss_count = self._semantic_hit_count = 0

    def _make_key(self, path, body):
        if not self._enabled() or path not in (
            "/v1/chat/completions", "/chat/completions"
        ):
            return ""
        if body.get("stream") or body.get("tools") or body.get("tool_choice"):
            return ""
        cacheable = {
            "model", "messages", "temperature", "top_p", "max_tokens",
            "presence_penalty", "frequency_penalty", "response_format",
            "thinking", "reasoning_effort",
        }
        payload = {key: body.get(key) for key in cacheable if key in body}
        if payload.get("temperature") == 1.0:
            del payload["temperature"]
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    @staticmethod
    def _last_user_message(body):
        messages = body.get("messages", [])
        if not isinstance(messages, list):
            return ""
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "user":
                content = message.get("content", "")
                return content if isinstance(content, str) else ""
        return ""

    def get(self, path, body, count_miss=True):
        key = self._make_key(path, body)
        if not key:
            return None
        with self._lock:
            entry = self._cache.get(key)
            if not entry:
                if count_miss:
                    self._miss_count += 1
                return None
            expires_at, data, headers = entry
            if time.time() > expires_at:
                del self._cache[key]
                self._signatures.pop(key, None)
                if count_miss:
                    self._miss_count += 1
                return None
            self._hit_count += 1
            return data, headers

    def get_semantic(self, path, body):
        result = self.get(path, body, count_miss=False)
        if result:
            return result
        if not self._semantic_enabled():
            with self._lock:
                self._miss_count += 1
            return None
        key = self._make_key(path, body)
        signature = self._semantic_signature(body) if key else None
        if not signature:
            return None
        with self._lock:
            now = time.time()
            for cached_key, cached_signature in self._signatures.items():
                if cached_key not in self._cache:
                    continue
                if now <= self._cache[cached_key][0] and cached_signature == signature:
                    self._semantic_hit_count += 1
                    self._hit_count += 1
                    return self._cache[cached_key][1], self._cache[cached_key][2]
            self._miss_count += 1
        return None

    def _semantic_signature(self, body):
        query = self._last_user_message(body)
        if not query:
            return None
        normalized = query.replace("\r\n", "\n").strip()
        context = json.loads(json.dumps(body, ensure_ascii=False))
        for message in reversed(context.get("messages", [])):
            if isinstance(message, dict) and message.get("role") == "user":
                if not isinstance(message.get("content"), str):
                    return None
                message["content"] = "<normalized-user-message>"
                break
        raw = json.dumps(context, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest(), normalized

    def put(self, path, body, data, headers, status):
        key = self._make_key(path, body) if status == 200 else ""
        if not key:
            return
        with self._lock:
            if len(self._cache) >= self._max_entries:
                oldest = min(self._cache, key=lambda item: self._cache[item][0])
                del self._cache[oldest]
                self._signatures.pop(oldest, None)
            filtered = [(key, value) for key, value in headers
                        if key.lower() == "content-type"]
            self._cache[key] = (time.time() + self._ttl, data, filtered)
            self._signatures[key] = self._semantic_signature(body)

    def stats(self):
        with self._lock:
            total = self._hit_count + self._miss_count
            return {
                "enabled": self._enabled(),
                "semantic_enabled": self._semantic_enabled(),
                "entries": len(self._cache),
                "ttl": self._ttl,
                "max_entries": self._max_entries,
                "hit_count": self._hit_count,
                "semantic_hits": self._semantic_hit_count,
                "miss_count": self._miss_count,
                "hit_rate": round(self._hit_count / total, 4) if total else 0.0,
            }

    def clear(self):
        with self._lock:
            self._cache.clear()
            self._signatures.clear()
            self._hit_count = self._semantic_hit_count = self._miss_count = 0

    def configure(self, ttl, max_entries):
        with self._lock:
            self._ttl = ttl
            self._max_entries = max_entries
            while len(self._cache) > max_entries:
                oldest = min(self._cache, key=lambda item: self._cache[item][0])
                del self._cache[oldest]
                self._signatures.pop(oldest, None)
