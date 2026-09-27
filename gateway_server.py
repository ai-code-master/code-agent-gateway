#!/usr/bin/env python3
"""
Kimi Code OAuth Proxy v3.0

A local HTTP proxy that bridges OpenAI-compatible clients (like Hermes)
to the Kimi Code API with automatic OAuth token refresh.

Changelog v3.0:
- Single-flight request coalescing: concurrent identical requests share one upstream call
- Lightweight semantic cache: similar requests (not just identical) hit cache
- Token usage tracking: input/output/total tokens recorded per request

Changelog v2.9:
- Response cache: identical non-streaming requests return cached result (saves 100% token)
- Message truncation: auto-truncate long conversation history to reduce input tokens

Changelog v2.8:
- Dynamic max_tokens: smaller requests get smaller max_tokens
- Model list cache: avoids repeated upstream /v1/models queries
- Hot-reload .env via SIGHUP or /admin/reload endpoint
- Admin API for runtime config inspection
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from gateway.config import GatewayConfig

# ==================== Configuration ====================
def _env(key, default=""):
    return os.environ.get(key, default)


_DOTENV_KEYS = set()


def _load_dotenv(path=".env", override=False):
    try:
        values = {}
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    values[k] = v.strip()
        if override:
            for key in _DOTENV_KEYS - values.keys():
                os.environ.pop(key, None)
                _DOTENV_KEYS.discard(key)
        for k, value in values.items():
            if k in _DOTENV_KEYS or k not in os.environ:
                os.environ[k] = value
                _DOTENV_KEYS.add(k)
    except Exception:
        pass


def _reload_config():
    """Re-read .env and update runtime globals."""
    global CONFIG
    global MAX_CONCURRENT, RPM_LIMIT, MAX_RETRIES, BACKOFF_BASE
    global REFRESH_INTERVAL, REFRESH_THRESHOLD, UPSTREAM_TIMEOUT
    global QUEUE_TIMEOUT, MAX_BODY_SIZE, SLOW_REQUEST_THRESHOLD
    global GRACEFUL_SHUTDOWN_WAIT, DEBUG_BODY, LOG_DIR_MAX_BYTES

    _script_dir = os.path.dirname(os.path.abspath(__file__))
    _load_dotenv(os.path.join(_script_dir, ".env"), override=True)
    CONFIG = GatewayConfig.from_env()

    MAX_CONCURRENT   = int(_env("KCP_MAX_CONCURRENT", "30"))
    RPM_LIMIT        = int(_env("KCP_RPM_LIMIT", "0"))
    MAX_RETRIES      = int(_env("KCP_MAX_RETRIES", "2"))
    BACKOFF_BASE     = float(_env("KCP_BACKOFF_BASE", "1.0"))
    REFRESH_INTERVAL = int(_env("KCP_REFRESH_INTERVAL", "300"))
    REFRESH_THRESHOLD = int(_env("KCP_REFRESH_THRESHOLD", "300"))
    UPSTREAM_TIMEOUT = int(_env("KCP_UPSTREAM_TIMEOUT", "600"))
    QUEUE_TIMEOUT    = int(_env("KCP_QUEUE_TIMEOUT", "300"))
    MAX_BODY_SIZE    = int(_env("KCP_MAX_BODY_SIZE", str(50 * 1024 * 1024)))
    SLOW_REQUEST_THRESHOLD = float(_env("KCP_SLOW_REQUEST_THRESHOLD", "30.0"))
    GRACEFUL_SHUTDOWN_WAIT = int(_env("KCP_GRACEFUL_SHUTDOWN_WAIT", "30"))
    DEBUG_BODY       = _env("KCP_DEBUG_BODY", "").lower() in ("1", "true", "yes")
    LOG_DIR_MAX_BYTES = int(_env("KCP_LOG_DIR_MAX_BYTES", str(500 * 1024 * 1024)))

    global ENABLE_CACHE, CACHE_TTL, CACHE_MAX_ENTRIES
    global ENABLE_TRUNCATE, MAX_HISTORY_PAIRS, MAX_ASSISTANT_CHARS
    global ENABLE_SINGLE_FLIGHT, SINGLE_FLIGHT_TIMEOUT
    global ENABLE_SEMANTIC_CACHE
    ENABLE_CACHE     = _env("KCP_ENABLE_CACHE", "1").lower() in ("1", "true", "yes")
    CACHE_TTL        = int(_env("KCP_CACHE_TTL", "300"))
    CACHE_MAX_ENTRIES = int(_env("KCP_CACHE_MAX_ENTRIES", "100"))
    ENABLE_TRUNCATE  = _env("KCP_ENABLE_TRUNCATE", "1").lower() in ("1", "true", "yes")
    MAX_HISTORY_PAIRS = int(_env("KCP_MAX_HISTORY_PAIRS", "10"))
    MAX_ASSISTANT_CHARS = int(_env("KCP_MAX_ASSISTANT_CHARS", "2000"))
    ENABLE_SINGLE_FLIGHT = _env("KCP_ENABLE_SINGLE_FLIGHT", "1").lower() in ("1", "true", "yes")
    SINGLE_FLIGHT_TIMEOUT = int(_env("KCP_SINGLE_FLIGHT_TIMEOUT", "30"))
    ENABLE_SEMANTIC_CACHE = _env("KCP_ENABLE_SEMANTIC_CACHE", "1").lower() in ("1", "true", "yes")
    logger.info("Config reloaded")


# Auto-load .env from the same directory as this script
_script_dir = os.path.dirname(os.path.abspath(__file__))
_load_dotenv(os.path.join(_script_dir, ".env"))
CONFIG = GatewayConfig.from_env()

# Paths
CREDENTIALS_PATH = _env("KCP_CREDENTIALS_PATH", os.path.expanduser("~/.kimi-code/credentials/kimi-code.json"))
DEVICE_ID_PATH   = _env("KCP_DEVICE_ID_PATH",   os.path.expanduser("~/.kimi-code/device_id"))

# Endpoints
AUTH_ENDPOINT    = _env("KCP_AUTH_ENDPOINT",    "https://auth.kimi.com/api/oauth/token")
UPSTREAM_BASE    = _env("KCP_UPSTREAM_BASE",    "https://api.kimi.com/coding")
CLIENT_ID        = _env("KCP_CLIENT_ID",        "")

# Server
PROXY_HOST       = _env("KCP_HOST", "127.0.0.1")
PROXY_PORT       = int(_env("KCP_PORT", "8765"))
MAX_CONCURRENT   = int(_env("KCP_MAX_CONCURRENT", "30"))
RPM_LIMIT        = int(_env("KCP_RPM_LIMIT", "0"))
MAX_RETRIES      = int(_env("KCP_MAX_RETRIES", "2"))
BACKOFF_BASE     = float(_env("KCP_BACKOFF_BASE", "1.0"))
REFRESH_INTERVAL = int(_env("KCP_REFRESH_INTERVAL", "300"))
REFRESH_THRESHOLD = int(_env("KCP_REFRESH_THRESHOLD", "300"))
UPSTREAM_TIMEOUT = int(_env("KCP_UPSTREAM_TIMEOUT", "600"))
QUEUE_TIMEOUT    = int(_env("KCP_QUEUE_TIMEOUT", "300"))
MAX_BODY_SIZE    = int(_env("KCP_MAX_BODY_SIZE", str(50 * 1024 * 1024)))  # 50MB
SLOW_REQUEST_THRESHOLD = float(_env("KCP_SLOW_REQUEST_THRESHOLD", "30.0"))
GRACEFUL_SHUTDOWN_WAIT = int(_env("KCP_GRACEFUL_SHUTDOWN_WAIT", "30"))
DEBUG_BODY       = _env("KCP_DEBUG_BODY", "").lower() in ("1", "true", "yes")

# Cache & Truncation
ENABLE_CACHE     = _env("KCP_ENABLE_CACHE", "1").lower() in ("1", "true", "yes")
CACHE_TTL        = int(_env("KCP_CACHE_TTL", "300"))
CACHE_MAX_ENTRIES = int(_env("KCP_CACHE_MAX_ENTRIES", "100"))
ENABLE_TRUNCATE  = _env("KCP_ENABLE_TRUNCATE", "1").lower() in ("1", "true", "yes")
MAX_HISTORY_PAIRS = int(_env("KCP_MAX_HISTORY_PAIRS", "10"))
MAX_ASSISTANT_CHARS = int(_env("KCP_MAX_ASSISTANT_CHARS", "2000"))

# Single-flight & Semantic cache
ENABLE_SINGLE_FLIGHT = _env("KCP_ENABLE_SINGLE_FLIGHT", "1").lower() in ("1", "true", "yes")
SINGLE_FLIGHT_TIMEOUT = int(_env("KCP_SINGLE_FLIGHT_TIMEOUT", "30"))
ENABLE_SEMANTIC_CACHE = _env("KCP_ENABLE_SEMANTIC_CACHE", "1").lower() in ("1", "true", "yes")

# Logging
LOG_DIR          = _env("KCP_LOG_DIR", os.path.expanduser("~/.hermes/logs"))
LOG_FILE         = os.path.join(LOG_DIR, "kimi-proxy.log")
LOG_MAX_BYTES    = int(_env("KCP_LOG_MAX_BYTES", str(10 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(_env("KCP_LOG_BACKUP_COUNT", "3"))
LOG_DIR_MAX_BYTES = int(_env("KCP_LOG_DIR_MAX_BYTES", str(500 * 1024 * 1024)))

# Device info (override to avoid leaking real machine names)
DEVICE_NAME      = _env("KCP_DEVICE_NAME",      "CodeAgentGateway")
DEVICE_MODEL     = _env("KCP_DEVICE_MODEL",     "Desktop")
DEVICE_PLATFORM  = _env("KCP_DEVICE_PLATFORM",  "macOS")
DEVICE_VERSION   = _env("KCP_DEVICE_VERSION",   "2.1.153")

# ==================== Validation ====================
if not CLIENT_ID:
    print("ERROR: KCP_CLIENT_ID is required. Set it in your .env file.", file=sys.stderr)
    sys.exit(1)

# ==================== Logging ====================
os.makedirs(LOG_DIR, exist_ok=True)


def _ensure_disk_space():
    """If log dir is over limit, delete oldest backup files."""
    try:
        total = 0
        files = []
        for entry in os.listdir(LOG_DIR):
            path = os.path.join(LOG_DIR, entry)
            if os.path.isfile(path) and "kimi-proxy" in entry:
                s = os.path.getsize(path)
                total += s
                files.append((path, os.path.getmtime(path), s))
        if total > LOG_DIR_MAX_BYTES:
            files.sort(key=lambda x: x[1])  # oldest first
            for path, mtime, s in files:
                if total <= LOG_DIR_MAX_BYTES * 0.8:
                    break
                try:
                    os.remove(path)
                    total -= s
                    print(f"[kimi-proxy] Disk guard: removed old log {path}", file=sys.stderr)
                except Exception:
                    pass
    except Exception:
        pass


class RotatingLogHandler(logging.Handler):
    def __init__(self, filename, max_bytes, backup_count):
        super().__init__()
        self.filename = filename
        self.max_bytes = max_bytes
        self.backup_count = backup_count
        self.stream = None
        self._lock = threading.Lock()
        self._open()

    def _open(self):
        if self.stream:
            self.stream.close()
        self.stream = open(self.filename, "a", encoding="utf-8")

    def _rotate(self):
        if os.path.exists(self.filename) and os.path.getsize(self.filename) >= self.max_bytes:
            self.stream.close()
            for i in range(self.backup_count - 1, 0, -1):
                src, dst = f"{self.filename}.{i}", f"{self.filename}.{i+1}"
                if os.path.exists(src):
                    os.replace(src, dst)
            if os.path.exists(self.filename):
                os.replace(self.filename, f"{self.filename}.1")
            self._open()

    def emit(self, record):
        try:
            with self._lock:
                _ensure_disk_space()
                self._rotate()
                self.stream.write(self.format(record) + "\n")
                self.stream.flush()
        except Exception:
            pass


_handler = RotatingLogHandler(LOG_FILE, LOG_MAX_BYTES, LOG_BACKUP_COUNT)
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger = logging.getLogger("kimi-proxy")
logger.setLevel(logging.INFO)
logger.addHandler(_handler)

# Also capture uncaught exceptions from threads into our log
def _log_uncaught(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, (SystemExit, KeyboardInterrupt)):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.error("Uncaught exception: %s", exc_value, exc_info=(exc_type, exc_value, exc_traceback))


sys.excepthook = _log_uncaught

# ==================== Metrics ====================
class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self.request_count = 0
        self.error_count = 0
        self.timeout_count = 0
        self.retry_count = 0
        self.client_reset_count = 0
        self.slow_request_count = 0
        self.latency_sum = 0.0
        self.latency_count = 0
        self.status_counts = {}
        self.queue_wait_sum = 0.0
        self.queue_wait_count = 0
        self.model_counts = {}
        # Token usage tracking
        self.input_tokens_sum = 0
        self.output_tokens_sum = 0
        self.total_tokens_sum = 0
        self.token_record_count = 0

    def record_request(self, latency: float, status: int | None = None, queue_wait: float = 0.0, model: str = "", tokens: dict | None = None):
        with self._lock:
            self.request_count += 1
            self.latency_sum += latency
            self.latency_count += 1
            if queue_wait > 0:
                self.queue_wait_sum += queue_wait
                self.queue_wait_count += 1
            if status is not None:
                self.status_counts[status] = self.status_counts.get(status, 0) + 1
                if status >= 500 or status == 429:
                    self.error_count += 1
            if model:
                self.model_counts[model] = self.model_counts.get(model, 0) + 1
            if latency > SLOW_REQUEST_THRESHOLD:
                self.slow_request_count += 1
            if tokens:
                self.input_tokens_sum += tokens.get("input", 0)
                self.output_tokens_sum += tokens.get("output", 0)
                self.total_tokens_sum += tokens.get("total", 0)
                self.token_record_count += 1

    def record_timeout(self):
        with self._lock:
            self.timeout_count += 1
            self.error_count += 1

    def record_retry(self):
        with self._lock:
            self.retry_count += 1

    def record_client_reset(self):
        with self._lock:
            self.client_reset_count += 1

    def snapshot(self):
        with self._lock:
            avg = (self.latency_sum / self.latency_count) if self.latency_count else 0.0
            avg_queue = (self.queue_wait_sum / self.queue_wait_count) if self.queue_wait_count else 0.0
            return {
                "request_count": self.request_count,
                "error_count": self.error_count,
                "timeout_count": self.timeout_count,
                "retry_count": self.retry_count,
                "client_reset_count": self.client_reset_count,
                "slow_request_count": self.slow_request_count,
                "slow_threshold_s": SLOW_REQUEST_THRESHOLD,
                "avg_latency_ms": round(avg * 1000, 2),
                "avg_queue_wait_ms": round(avg_queue * 1000, 2),
                "status_counts": dict(self.status_counts),
                "model_counts": dict(self.model_counts),
                "tokens": {
                    "input_total": self.input_tokens_sum,
                    "output_total": self.output_tokens_sum,
                    "total": self.total_tokens_sum,
                    "recorded_requests": self.token_record_count,
                },
            }


metrics = Metrics()

# ==================== RPM Limiter ====================
class RPMLimiter:
    """Simple sliding-window RPM limiter."""

    def __init__(self, max_rpm: int):
        self._max_rpm = max_rpm
        self._lock = threading.Lock()
        self._timestamps = []

    def allow(self) -> bool:
        if self._max_rpm <= 0:
            return True
        now = time.time()
        window_start = now - 60
        with self._lock:
            self._timestamps = [t for t in self._timestamps if t > window_start]
            if len(self._timestamps) >= self._max_rpm:
                return False
            self._timestamps.append(now)
            return True

    def current(self) -> int:
        now = time.time()
        window_start = now - 60
        with self._lock:
            self._timestamps = [t for t in self._timestamps if t > window_start]
            return len(self._timestamps)


rpm_limiter = RPMLimiter(RPM_LIMIT)

# ==================== Concurrency Control ====================
kimi_semaphore = threading.Semaphore(MAX_CONCURRENT)
_active_requests_lock = threading.Lock()
_active_requests = 0
_shutdown_event = threading.Event()

# ==================== Model List Cache ====================
class ModelListCache:
    """Cache upstream /v1/models to avoid repeated queries."""

    def __init__(self, ttl: int = 300):
        self._ttl = ttl
        self._lock = threading.Lock()
        self._data = None
        self._expires_at = 0

    def get(self, token: str) -> bytes | None:
        with self._lock:
            if self._data and time.time() < self._expires_at:
                return self._data
        # Cache miss or expired — fetch from upstream
        try:
            parsed = urllib.parse.urlparse(UPSTREAM_BASE)
            conn = http.client.HTTPSConnection(parsed.netloc, timeout=10)
            try:
                conn.request(
                    "GET",
                    f"{parsed.path}/v1/models",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "User-Agent": "KimiCLI/1.5",
                    },
                )
                resp = conn.getresponse()
                data = resp.read()
                if resp.status == 200:
                    with self._lock:
                        self._data = data
                        self._expires_at = time.time() + self._ttl
                    return data
            finally:
                conn.close()
        except Exception as e:
            logger.debug(f"Model list fetch failed: {e}")
        return None


model_cache = ModelListCache(ttl=300)

# ==================== Response Cache (chat completions) ====================
class ResponseCache:
    """In-memory cache for LLM non-streaming responses.
    
    Supports exact caching and conservative normalized-equivalence matching.
    """

    def __init__(self, ttl: int = 300, max_entries: int = 100):
        self._ttl = ttl
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._cache = {}       # key -> (expires_at, data_bytes, headers_list)
        self._signatures = {}  # key -> (model, last_user_msg, temperature)
        self._hit_count = 0
        self._miss_count = 0
        self._semantic_hit_count = 0

    def _make_key(self, path: str, body_dict: dict) -> str:
        """Create cache key; return '' if request should not be cached."""
        if not ENABLE_CACHE:
            return ""
        if path not in ("/v1/chat/completions", "/chat/completions"):
            return ""
        if body_dict.get("stream"):
            return ""
        if body_dict.get("tools") or body_dict.get("tool_choice"):
            return ""
        cacheable = {"model", "messages", "temperature", "top_p",
                     "max_tokens", "presence_penalty", "frequency_penalty",
                     "response_format", "thinking", "reasoning_effort"}
        payload = {k: body_dict.get(k) for k in cacheable if k in body_dict}
        if payload.get("temperature") == 1.0:
            del payload["temperature"]
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _last_user_message(self, body_dict: dict) -> str:
        messages = body_dict.get("messages", [])
        if not isinstance(messages, list):
            return ""
        for msg in reversed(messages):
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content
        return ""

    def get(self, path: str, body_dict: dict, count_miss=True) -> tuple[bytes, list] | None:
        key = self._make_key(path, body_dict)
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
                del self._signatures[key]
                if count_miss:
                    self._miss_count += 1
                return None
            self._hit_count += 1
            return data, headers

    def get_semantic(self, path: str, body_dict: dict) -> tuple[bytes, list] | None:
        """Match requests differing only by newlines or outer whitespace."""
        result = self.get(path, body_dict, count_miss=False)
        if result:
            return result
        if not ENABLE_SEMANTIC_CACHE:
            with self._lock:
                self._miss_count += 1
            return None
        key = self._make_key(path, body_dict)
        if not key:
            return None
        signature = self._semantic_signature(body_dict)
        if not signature:
            return None
        with self._lock:
            now = time.time()
            for ck, sig in self._signatures.items():
                if ck not in self._cache:
                    continue
                expires_at = self._cache[ck][0]
                if now <= expires_at and sig == signature:
                    self._semantic_hit_count += 1
                    self._hit_count += 1
                    return self._cache[ck][1], self._cache[ck][2]
            self._miss_count += 1
        return None

    def _semantic_signature(self, body_dict: dict):
        query = self._last_user_message(body_dict)
        if not query:
            return None
        normalized = query.replace("\r\n", "\n").strip()
        context = json.loads(json.dumps(body_dict, ensure_ascii=False))
        for message in reversed(context.get("messages", [])):
            if isinstance(message, dict) and message.get("role") == "user":
                if not isinstance(message.get("content"), str):
                    return None
                message["content"] = "<normalized-user-message>"
                break
        raw = json.dumps(context, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest(), normalized

    def put(self, path: str, body_dict: dict, data: bytes, headers: list, status: int):
        if status != 200:
            return
        key = self._make_key(path, body_dict)
        if not key:
            return
        with self._lock:
            if len(self._cache) >= self._max_entries:
                oldest = min(self._cache.keys(), key=lambda k: self._cache[k][0])
                del self._cache[oldest]
                del self._signatures[oldest]
            filtered = [(h, v) for h, v in headers if h.lower() == "content-type"]
            self._cache[key] = (time.time() + self._ttl, data, filtered)
            self._signatures[key] = self._semantic_signature(body_dict)

    def stats(self):
        with self._lock:
            total = self._hit_count + self._miss_count
            return {
                "enabled": ENABLE_CACHE,
                "semantic_enabled": ENABLE_SEMANTIC_CACHE,
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
            self._hit_count = 0
            self._semantic_hit_count = 0
            self._miss_count = 0

    def configure(self, ttl, max_entries):
        with self._lock:
            self._ttl = ttl
            self._max_entries = max_entries
            while len(self._cache) > max_entries:
                oldest = min(self._cache, key=lambda k: self._cache[k][0])
                del self._cache[oldest]
                self._signatures.pop(oldest, None)


response_cache = ResponseCache(ttl=CACHE_TTL, max_entries=CACHE_MAX_ENTRIES)

# ==================== Single-Flight Request Coalescing ====================
class SingleFlight:
    """Coalesce concurrent identical requests into a single upstream call."""

    def __init__(self):
        self._lock = threading.Lock()
        self._inflight = {}  # key -> (condition, result, error, done)

    def _make_key(self, method: str, path: str, body: bytes) -> str:
        if not ENABLE_SINGLE_FLIGHT:
            return ""
        if method != "POST" or path not in ("/v1/chat/completions", "/chat/completions"):
            return ""
        try:
            if json.loads(body).get("stream"):
                return ""
        except (AttributeError, json.JSONDecodeError):
            return ""
        return hashlib.sha256(f"{method}:{path}:".encode() + body).hexdigest()

    def do(self, method: str, path: str, body: bytes, callable_fn):
        """Execute callable_fn, coalescing with concurrent identical requests.
        Returns (result, is_coalesced)."""
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
            if not entry["event"].wait(timeout=SINGLE_FLIGHT_TIMEOUT):
                return callable_fn(), False
            if entry["error"]:
                raise entry["error"]
            return entry["result"], True
        try:
            result = callable_fn()
            entry["result"] = result
            return result, False
        except Exception as e:
            entry["error"] = e
            raise
        finally:
            entry["event"].set()
            with self._lock:
                if self._inflight.get(key) is entry:
                    del self._inflight[key]


single_flight = SingleFlight()
class UpstreamHealth:
    def __init__(self):
        self._lock = threading.Lock()
        self._healthy = True
        self._last_check = 0
        self._check_interval = 30
        self._consecutive_failures = 0
        self._failure_threshold = 2

    def _do_probe(self):
        try:
            token = token_mgr.get_token()
            if not token:
                return False
            parsed = urllib.parse.urlparse(UPSTREAM_BASE)
            conn = http.client.HTTPSConnection(parsed.netloc, timeout=5)
            try:
                conn.request(
                    "GET",
                    f"{parsed.path}/v1/models",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "User-Agent": "KimiCLI/1.5",
                    },
                )
                resp = conn.getresponse()
                result = "healthy" if resp.status == 200 else (
                    "auth_failure" if resp.status == 401 else "failure"
                )
            finally:
                conn.close()
            return result
        except Exception as e:
            logger.debug(f"Upstream probe failed: {e}")
            return "failure"

    def check(self):
        with self._lock:
            if time.time() - self._last_check < self._check_interval:
                return self._healthy
        result = self._do_probe()
        with self._lock:
            if result == "healthy":
                self._consecutive_failures = 0
                healthy = True
            elif result == "auth_failure":
                self._consecutive_failures = self._failure_threshold
                healthy = False
            else:
                self._consecutive_failures += 1
                healthy = (
                    self._healthy
                    if self._consecutive_failures < self._failure_threshold
                    else False
                )
            if healthy != self._healthy:
                if healthy:
                    logger.info("Upstream health probe: healthy")
                else:
                    logger.warning(
                        f"Upstream health probe: UNHEALTHY ({result})"
                    )
            self._healthy = healthy
            self._last_check = time.time()
        return healthy

    def is_healthy(self):
        with self._lock:
            if time.time() - self._last_check > self._check_interval * 3:
                return False
            return self._healthy


upstream_health = UpstreamHealth()


def _upstream_probe_worker():
    check_interval = upstream_health._check_interval
    while not _shutdown_event.is_set():
        upstream_health.check()
        for _ in range(check_interval):
            if _shutdown_event.is_set():
                break
            time.sleep(1)


# ==================== Token Management ====================
class TokenManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._refreshing = False
        self._refresh_cond = threading.Condition(self._refresh_lock)
        self._data = {}
        self._mtime = None
        self._device_id = self._load_device_id()
        self._device_info = self._load_device_info()
        self._load()

    def _load_device_id(self):
        try:
            with open(DEVICE_ID_PATH) as f:
                return f.read().strip()
        except Exception:
            return "unknown"

    def _load_device_info(self):
        import platform
        return {
            "platform": DEVICE_PLATFORM,
            "version": DEVICE_VERSION,
            "device_name": DEVICE_NAME,
            "device_model": DEVICE_MODEL,
            "os_version": platform.mac_ver()[0] or _env("KCP_OS_VERSION", "15.0"),
            "device_id": self._device_id,
        }

    def _load(self):
        try:
            with open(CREDENTIALS_PATH) as f:
                self._data = json.load(f)
            self._mtime = os.path.getmtime(CREDENTIALS_PATH)
            remaining = max(0, self._data.get("expires_at", 0) - time.time())
            logger.info(f"Token loaded, remaining {remaining:.0f}s")
        except Exception as e:
            logger.error(f"Load token failed: {e}")
            self._data = {}

    def _maybe_reload(self):
        """Reload credentials if the file changed on disk (e.g. CLI re-login)."""
        try:
            mtime = os.path.getmtime(CREDENTIALS_PATH)
        except OSError:
            return
        if self._mtime is not None and mtime != self._mtime:
            logger.info("Credentials file changed on disk, reloading")
            self._load()

    def _save(self):
        try:
            with open(CREDENTIALS_PATH, "w") as f:
                json.dump(self._data, f, indent=2)
            self._mtime = os.path.getmtime(CREDENTIALS_PATH)
        except Exception as e:
            logger.error(f"Save token failed: {e}")

    def get_token(self):
        with self._lock:
            self._maybe_reload()
            return self._data.get("access_token", "")

    def get_headers(self):
        d = self._device_info
        return {
            "X-Msh-Platform": d["platform"],
            "X-Msh-Version": d["version"],
            "X-Msh-Device-Name": d["device_name"],
            "X-Msh-Device-Model": d["device_model"],
            "X-Msh-Os-Version": d["os_version"],
            "X-Msh-Device-Id": d["device_id"],
        }

    def should_refresh(self):
        with self._lock:
            self._maybe_reload()
            return (self._data.get("expires_at", 0) - time.time()) < REFRESH_THRESHOLD

    def refresh(self):
        with self._refresh_lock:
            if self._refreshing:
                logger.info("Waiting for refresh...")
                self._refresh_cond.wait(timeout=30)
                with self._lock:
                    return self._data.get("expires_at", 0) > time.time() + 10
            self._refreshing = True
        try:
            with self._lock:
                self._maybe_reload()
                refresh_token = self._data.get("refresh_token", "")
                if not refresh_token:
                    logger.warning("No refresh_token available")
                    return False
                parsed_auth = urllib.parse.urlparse(AUTH_ENDPOINT)
                conn = http.client.HTTPSConnection(parsed_auth.netloc, timeout=30)
                try:
                    body = urllib.parse.urlencode({
                        "grant_type": "refresh_token",
                        "client_id": CLIENT_ID,
                        "refresh_token": refresh_token,
                    })
                    conn.request(
                        "POST",
                        parsed_auth.path,
                        body=body,
                        headers={"Content-Type": "application/x-www-form-urlencoded"},
                    )
                    resp = conn.getresponse()
                    resp_body = resp.read()
                    if resp.status != 200:
                        try:
                            err_summary = json.loads(resp_body)
                        except Exception:
                            err_summary = resp_body.decode("utf-8", errors="replace")[:200]
                        logger.error(f"Token refresh HTTP {resp.status}: {err_summary}")
                        return False
                    new_tokens = json.loads(resp_body)
                finally:
                    conn.close()
                self._data["access_token"] = new_tokens["access_token"]
                self._data["refresh_token"] = new_tokens["refresh_token"]
                self._data["token_type"] = new_tokens.get("token_type", "Bearer")
                self._data["expires_in"] = new_tokens.get("expires_in", 900)
                self._data["expires_at"] = time.time() + new_tokens.get("expires_in", 900)
                self._save()
                logger.info("Token refresh OK")
                return True
        except Exception as e:
            logger.error(f"Token refresh failed: {e}")
            return False
        finally:
            with self._refresh_lock:
                self._refreshing = False
                self._refresh_cond.notify_all()


token_mgr = TokenManager()

# ==================== Background Threads ====================
def refresh_worker():
    while not _shutdown_event.is_set():
        for _ in range(REFRESH_INTERVAL):
            if _shutdown_event.is_set():
                return
            time.sleep(1)
        if token_mgr.should_refresh():
            logger.info("Token expiring, refreshing...")
            token_mgr.refresh()


# ==================== Error Classification ====================
def classify_error(e):
    """Classify an exception into a user-friendly error type."""
    if isinstance(e, socket.timeout):
        return "upstream_timeout", "Upstream read timed out (model may be thinking too long)"
    if isinstance(e, ConnectionResetError):
        return "connection_reset", "Upstream closed connection unexpectedly"
    if isinstance(e, BrokenPipeError):
        return "broken_pipe", "Connection broken while sending request"
    if isinstance(e, ssl.SSLError):
        return "ssl_error", f"TLS/SSL error: {e}"
    if isinstance(e, OSError) and e.errno in (61, 111, 51, 8):
        return "connection_refused", "Cannot connect to upstream (network or DNS issue)"
    err_name = type(e).__name__
    return err_name.lower(), str(e)


# ==================== Dynamic max_tokens ====================
def _estimate_tokens(body_dict: dict) -> int:
    """Roughly estimate input token count from messages."""
    messages = body_dict.get("messages", [])
    if not isinstance(messages, list):
        return 0
    total_chars = 0
    for msg in messages:
        if isinstance(msg, dict):
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and "text" in part:
                        total_chars += len(part["text"])
    # Rough heuristic: 1 token ≈ 1.5 Chinese chars or 4 English chars
    # Use a blended estimate
    return total_chars // 2


def _dynamic_max_tokens(body_dict: dict) -> int:
    """Return a max_tokens value based on estimated input size."""
    est = _estimate_tokens(body_dict)
    if est < 2000:
        return 4096
    if est < 8000:
        return 8192
    if est < 16000:
        return 16384
    return 32768


# ==================== Request Body Helpers ====================
_THINKING_BUDGET_MAP = {
    "low": 4000,
    "medium": 8000,
    "high": 16000,
}


def _maybe_inject_thinking(body_dict: dict) -> dict:
    """If client sends reasoning_effort without thinking, inject thinking param."""
    if not isinstance(body_dict, dict):
        return body_dict
    if "thinking" in body_dict:
        return body_dict
    effort = body_dict.get("reasoning_effort")
    if isinstance(effort, str):
        effort = effort.strip().lower()
        budget = _THINKING_BUDGET_MAP.get(effort, 8000)
        body_dict["thinking"] = {"type": "enabled", "budget_tokens": budget}
        logger.info(f"Injected thinking=budget_tokens:{budget} from reasoning_effort={effort}")
    return body_dict


def _safe_body_preview(body: bytes, max_len: int = 500) -> str:
    """Return a safe preview of body for debug logging."""
    return body[:max_len].decode("utf-8", errors="replace")


def _truncate_messages(body_dict: dict) -> dict:
    """Truncate conversation history to reduce input tokens.

    Strategy:
    1. Keep system messages intact.
    2. Keep last N user-assistant pairs (configurable via KCP_MAX_HISTORY_PAIRS).
       When dropping old messages, ensure tool_call_id references remain valid:
       if a tool message is kept, the assistant message that issued its tool_call
       must also be kept.
    3. Truncate very long individual assistant messages.
    """
    if not ENABLE_TRUNCATE:
        return body_dict
    messages = body_dict.get("messages", [])
    if not isinstance(messages, list) or len(messages) <= 2:
        return body_dict

    system_msgs = []
    conv_msgs = []
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            system_msgs.append(msg)
        else:
            conv_msgs.append(msg)

    # Build map: tool_call_id -> index of assistant message that contains it
    tool_call_assistant_map = {}
    for idx, msg in enumerate(conv_msgs):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            for tc in msg.get("tool_calls", []):
                if isinstance(tc, dict) and "id" in tc:
                    tool_call_assistant_map[tc["id"]] = idx

    # Keep last N pairs (2 messages per pair)
    keep_count = max(MAX_HISTORY_PAIRS * 2, 4)  # at least 4 messages
    truncated_info = ""
    if len(conv_msgs) > keep_count:
        start_idx = len(conv_msgs) - keep_count
        drop_count = start_idx

        # Adjust start_idx backward so that any tool message kept has its
        # originating assistant message also kept.
        adjusted = True
        while adjusted:
            adjusted = False
            required_assistants = set()
            for idx in range(start_idx, len(conv_msgs)):
                msg = conv_msgs[idx]
                if isinstance(msg, dict) and msg.get("role") == "tool":
                    tc_id = msg.get("tool_call_id")
                    if tc_id and tc_id in tool_call_assistant_map:
                        ast_idx = tool_call_assistant_map[tc_id]
                        if ast_idx < start_idx:
                            required_assistants.add(ast_idx)
            if required_assistants:
                start_idx = min(required_assistants)
                adjusted = True

        drop_count = start_idx
        conv_msgs = conv_msgs[start_idx:]
        if drop_count > 0:
            truncated_info = f" (dropped {drop_count} older messages)"

    # Truncate long assistant messages
    trunc_count = 0
    for msg in conv_msgs:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > MAX_ASSISTANT_CHARS:
                msg["content"] = content[:MAX_ASSISTANT_CHARS] + "\n\n[...truncated by proxy]"
                trunc_count += 1

    if truncated_info or trunc_count:
        logger.info(
            f"Message truncation:{truncated_info} assistant_msgs_truncated={trunc_count} "
            f"final_count={len(system_msgs) + len(conv_msgs)}"
        )
    body_dict["messages"] = system_msgs + conv_msgs
    return body_dict

# ==================== Core Request Function ====================
def _do_kimi_request(method, target_url, body, headers, retries=0):
    parsed = urllib.parse.urlparse(target_url)
    host, path = parsed.netloc, parsed.path + ("?" + parsed.query if parsed.query else "")
    proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy_url:
        parsed_proxy = urllib.parse.urlparse(proxy_url)
        proxy_host = parsed_proxy.hostname
        proxy_port = parsed_proxy.port or 7897
        conn = http.client.HTTPSConnection(proxy_host, proxy_port, timeout=UPSTREAM_TIMEOUT)
        conn.set_tunnel(host)
    else:
        conn = http.client.HTTPSConnection(host, timeout=UPSTREAM_TIMEOUT)
    try:
        req_headers = dict(headers)
        req_headers["Connection"] = "close"
        conn.request(method, path, body=body, headers=req_headers)
        resp = conn.getresponse()
        status = resp.status
        if status in (502, 429, 503) and retries < MAX_RETRIES:
            wait = BACKOFF_BASE * (2 ** retries)
            logger.warning(f"HTTP {status}, retry in {wait:.1f}s (attempt {retries+1})...")
            metrics.record_retry()
            time.sleep(wait)
            conn.close()
            return _do_kimi_request(method, target_url, body, headers, retries + 1)
        resp._conn = conn
        return resp, None
    except Exception as e:
        conn.close()
        return None, e


# ==================== Admin Config ====================
def _current_config():
    return {
        "max_concurrent": MAX_CONCURRENT,
        "rpm_limit": RPM_LIMIT,
        "upstream_timeout": UPSTREAM_TIMEOUT,
        "queue_timeout": QUEUE_TIMEOUT,
        "max_body_size": MAX_BODY_SIZE,
        "slow_request_threshold": SLOW_REQUEST_THRESHOLD,
        "debug_body": DEBUG_BODY,
        "cache": {
            "enabled": ENABLE_CACHE,
            "ttl": CACHE_TTL,
            "max_entries": CACHE_MAX_ENTRIES,
        },
        "truncation": {
            "enabled": ENABLE_TRUNCATE,
            "max_history_pairs": MAX_HISTORY_PAIRS,
            "max_assistant_chars": MAX_ASSISTANT_CHARS,
        },
        "single_flight": {
            "enabled": ENABLE_SINGLE_FLIGHT,
            "timeout": SINGLE_FLIGHT_TIMEOUT,
        },
        "semantic_cache": {
            "enabled": ENABLE_SEMANTIC_CACHE,
            "mode": "normalized_equivalence",
        },
    }


# ==================== HTTP Proxy ====================
class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Override to suppress default stderr logging
    def log_message(self, format, *args):
        if self.path not in ("/healthz", "/metrics", "/admin/config", "/admin/reload"):
            logger.info(f"{self.command} {self.path} -> {args[1]}")

    # Catch client disconnects early to avoid stderr spam
    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, TimeoutError) as e:
            client = self.client_address[0] if self.client_address else "unknown"
            logger.debug(f"Client {client} disconnected early: {type(e).__name__}")
            metrics.record_client_reset()
        except Exception as e:
            client = self.client_address[0] if self.client_address else "unknown"
            logger.error(f"Unhandled exception for client {client}: {e}", exc_info=True)

    def _client_ip(self):
        return self.client_address[0] if self.client_address else "unknown"

    def _send_error(self, code, message, extra_headers=None):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Connection", "close")
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(json.dumps({"error": message}).encode())

    def _forward(self, method):
        global _active_requests
        start_time = time.time()
        client_ip = self._client_ip()
        request_id = self.headers.get("x-request-id", "")
        if not request_id:
            request_id = f"kp-{uuid.uuid4().hex[:12]}"

        # --- Admin endpoints ---
        if self.path == "/admin/config":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(json.dumps(_current_config()).encode())
            return

        if self.path == "/admin/reload":
            _reload_config()
            global rpm_limiter, kimi_semaphore
            rpm_limiter = RPMLimiter(RPM_LIMIT)
            kimi_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT)
            response_cache.configure(CACHE_TTL, CACHE_MAX_ENTRIES)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "reloaded", "config": _current_config()}).encode())
            return

        token = token_mgr.get_token()
        if not token:
            latency = time.time() - start_time
            metrics.record_request(latency)
            logger.warning(f"[{request_id}] {client_ip} -> 401 (no token)")
            self._send_error(401, "No Kimi Code token available")
            return

        # --- RPM guard ---
        if not rpm_limiter.allow():
            latency = time.time() - start_time
            metrics.record_request(latency, 429)
            current_rpm = rpm_limiter.current()
            logger.warning(f"[{request_id}] {client_ip} -> 429 (RPM limit {current_rpm}/{RPM_LIMIT})")
            self._send_error(429, f"Rate limit exceeded: {current_rpm} requests in the last minute")
            return

        # --- Body size guard ---
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > MAX_BODY_SIZE:
            latency = time.time() - start_time
            metrics.record_request(latency, 413)
            logger.warning(f"[{request_id}] {client_ip} -> 413 (body {content_length} > {MAX_BODY_SIZE})")
            self._send_error(413, f"Request body too large: {content_length} bytes")
            return

        body = self.rfile.read(content_length) if content_length > 0 else b""
        body_len = len(body)

        if DEBUG_BODY and body:
            logger.debug(f"[{request_id}] Request body preview: {_safe_body_preview(body)}")

        path = self.path
        if path.startswith("/api/"):
            path = path[4:]
        if path.startswith("/v1/models/"):
            path = "/v1/models"

        # --- Model list cache for GET /v1/models ---
        if method == "GET" and path == "/v1/models":
            cached = model_cache.get(token)
            if cached:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(cached)
                self.wfile.flush()
                logger.info(f"[{request_id}] {client_ip} -> 200 (model_cache_hit)")
                return

        if path.startswith("/v1/"):
            target_url = f"{UPSTREAM_BASE}{path}"
        elif path == "/chat/completions":
            target_url = f"{UPSTREAM_BASE}/v1/chat/completions"
        elif path == "/models":
            target_url = f"{UPSTREAM_BASE}/v1/models"
        else:
            target_url = f"{UPSTREAM_BASE}/v1{path}"

        # --- Body enrichment ---
        model_name = ""
        req_json = {}
        try:
            req_json = json.loads(body) if body else {}
            if isinstance(req_json, dict):
                model_name = req_json.get("model", "")
                # Truncate long conversation history
                req_json = _truncate_messages(req_json)
                # Dynamic max_tokens based on estimated input size
                if "max_tokens" not in req_json:
                    suggested = _dynamic_max_tokens(req_json)
                    req_json["max_tokens"] = suggested
                    logger.info(f"[{request_id}] Dynamic max_tokens={suggested} (est_input ~{_estimate_tokens(req_json)} tokens)")
                else:
                    logger.debug(f"[{request_id}] Preserving max_tokens={req_json['max_tokens']}")
                req_json = _maybe_inject_thinking(req_json)
                body = json.dumps(req_json).encode("utf-8")
                body_len = len(body)
        except Exception as e:
            logger.debug(f"[{request_id}] Body enrichment skipped: {e}")
            if DEBUG_BODY and body:
                logger.debug(f"[{request_id}] Raw body preview: {_safe_body_preview(body)}")
        is_stream = bool(isinstance(req_json, dict) and req_json.get("stream"))

        # --- Response cache check ---
        if method == "POST" and req_json:
            cached = response_cache.get_semantic(path, req_json)
            if cached:
                data, resp_headers = cached
                self.send_response(200)
                for h, v in resp_headers:
                    self.send_header(h, v)
                self.send_header("X-Cache", "HIT")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(data)
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    logger.debug("Client disconnected during cached response write")
                latency = time.time() - start_time
                metrics.record_request(latency, 200, model=model_name)
                logger.info(f"[{request_id}] {client_ip} -> 200 (cache_hit) latency={latency:.2f}s model={model_name or '-'}")
                return

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "KimiCLI/1.5",
            "Accept": "text/event-stream" if is_stream else "application/json",
            "X-Request-Id": request_id,
        }
        headers.update(token_mgr.get_headers())
        for key in ("openai-beta", "anthropic-version"):
            if key in self.headers:
                headers[key] = self.headers[key]

        acquired = False
        request_semaphore = kimi_semaphore
        queue_start = time.time()
        try:
            acquired = request_semaphore.acquire(timeout=QUEUE_TIMEOUT)
            queue_wait = time.time() - queue_start
            if not acquired:
                latency = time.time() - start_time
                metrics.record_request(latency, 503, queue_wait)
                logger.warning(
                    f"[{request_id}] {client_ip} -> 503 (queue_full wait={queue_wait:.2f}s active={_active_requests}/{MAX_CONCURRENT})"
                )
                self._send_error(
                    503,
                    "Kimi API concurrency limit exceeded, try again later",
                    extra_headers={"Retry-After": str(min(120, UPSTREAM_TIMEOUT))},
                )
                return

            with _active_requests_lock:
                _active_requests += 1

            logger.info(
                f"[{request_id}] {client_ip} -> {method} {path} "
                f"body={body_len}b queue_wait={queue_wait:.2f}s active={_active_requests}/{MAX_CONCURRENT}"
            )

            def _do_request():
                resp, error = _do_kimi_request(method, target_url, body, headers)
                if not resp or resp.status != 401:
                    return resp, error
                logger.warning(f"[{request_id}] {client_ip} -> 401, refreshing token...")
                resp.close()
                if hasattr(resp, "_conn"):
                    resp._conn.close()
                if not token_mgr.refresh():
                    logger.error(f"[{request_id}] Token refresh failed")
                    return None, RuntimeError("Token refresh failed")
                new_headers = {
                    **headers,
                    "Authorization": f"Bearer {token_mgr.get_token()}",
                }
                retried, retry_error = _do_kimi_request(
                    method, target_url, body, new_headers
                )
                if retried and retried.status != 401:
                    logger.info(f"[{request_id}] Retry after refresh OK")
                else:
                    logger.error(f"[{request_id}] Retry after refresh failed")
                return retried, retry_error

            if is_stream:
                resp, error = _do_request()
                if resp:
                    status = resp.status
                    resp_len = self._send_stream_response(
                        status, resp.getheaders(), resp
                    )
                    latency = time.time() - start_time
                    metrics.record_request(latency, status, queue_wait, model_name)
                    logger.info(
                        f"[{request_id}] {client_ip} -> {status} (stream) "
                        f"latency={latency:.2f}s queue={queue_wait:.2f}s "
                        f"req={body_len}b resp={resp_len}b model={model_name or '-'}"
                    )
                    return
            else:
                def _buffered_request():
                    response, request_error = _do_request()
                    if not response:
                        return None, [], b"", request_error
                    try:
                        return (
                            response.status,
                            response.getheaders(),
                            response.read(),
                            None,
                        )
                    finally:
                        response.close()
                        if hasattr(response, "_conn"):
                            response._conn.close()

                try:
                    result, coalesced = single_flight.do(
                        method, path, body, _buffered_request
                    )
                    status, resp_headers, resp_body, error = result
                except Exception as e:
                    status, resp_headers, resp_body, error = None, [], b"", e
                    coalesced = False

            if not is_stream and status is not None:
                latency = time.time() - start_time
                resp_len = len(resp_body)
                tokens = {"input": 0, "output": 0, "total": 0}
                if resp_body:
                    try:
                        usage = json.loads(resp_body).get("usage", {})
                        tokens["input"] = usage.get("prompt_tokens", 0)
                        tokens["output"] = usage.get("completion_tokens", 0)
                        tokens["total"] = usage.get("total_tokens", 0)
                    except Exception:
                        pass
                if DEBUG_BODY and resp_body:
                    logger.debug(f"[{request_id}] Response body preview: {_safe_body_preview(resp_body)}")
                # Store successful response in cache
                if req_json and status == 200:
                    response_cache.put(path, req_json, resp_body, resp_headers, status)
                metrics.record_request(latency, status, queue_wait, model_name, tokens)
                coalesced_tag = " (coalesced)" if coalesced else ""
                log_level = logger.warning if latency > SLOW_REQUEST_THRESHOLD else logger.info
                log_level(
                    f"[{request_id}] {client_ip} -> {status}{coalesced_tag} "
                    f"latency={latency:.2f}s queue={queue_wait:.2f}s "
                    f"req={body_len}b resp={resp_len}b "
                    f"tokens={tokens['input']}+{tokens['output']}={tokens['total']} "
                    f"model={model_name or '-'}"
                )
                self._send_response(status, resp_headers, resp_body)
            else:
                latency = time.time() - start_time
                err_type, err_msg = classify_error(error)
                if err_type == "upstream_timeout":
                    metrics.record_timeout()
                else:
                    metrics.record_request(latency, 502, queue_wait, model_name)
                logger.error(
                    f"[{request_id}] {client_ip} -> 502 ({err_type}) "
                    f"latency={latency:.2f}s queue={queue_wait:.2f}s: {err_msg}"
                )
                self._send_error(502, f"{err_type}: {err_msg}")
        finally:
            if acquired:
                with _active_requests_lock:
                    _active_requests -= 1
                request_semaphore.release()

    def _send_response(self, status, resp_headers, data):
        self.send_response(status)
        for header, value in resp_headers:
            hl = header.lower()
            if hl in (
                "connection", "transfer-encoding", "content-length",
                "date", "server", "set-cookie",
            ):
                continue
            self.send_header(header, value)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Client disconnected during response write")
        finally:
            pass  # data already fully read; no conn to close here

    def _send_stream_response(self, status, resp_headers, resp):
        self.send_response(status)
        for header, value in resp_headers:
            if header.lower() in (
                "connection", "transfer-encoding", "content-length",
                "date", "server", "set-cookie",
            ):
                continue
            self.send_header(header, value)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        sent = 0
        try:
            while True:
                chunk = resp.readline()
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
                sent += len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            logger.debug("Client disconnected during streaming response")
            metrics.record_client_reset()
        finally:
            resp.close()
            if hasattr(resp, "_conn"):
                resp._conn.close()
        return sent

    def do_GET(self):
        if self.path == "/healthz":
            upstream_ok = upstream_health.is_healthy()
            self.send_response(200 if upstream_ok else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            features = [
                "connection_close", "backoff_retry", "auto_refresh_401",
                "full_xmsh_headers", "thinking_injection", "metrics",
                "structured_access_log", "client_reset_guard", "body_size_guard",
                "upstream_health_probe", "model_list_cache",
                "dynamic_max_tokens", "hot_reload", "token_tracking",
                "true_streaming",
            ]
            features.extend(name for enabled, name in (
                (DEBUG_BODY, "debug_body"),
                (ENABLE_CACHE, "response_cache"),
                (ENABLE_TRUNCATE, "message_truncation"),
                (ENABLE_SINGLE_FLIGHT, "single_flight"),
                (ENABLE_SEMANTIC_CACHE, "semantic_cache"),
            ) if enabled)
            health = {
                "status": "ok" if upstream_ok else "degraded",
                "upstream_healthy": upstream_ok,
                "version": "3.0",
                "token_expires_at": token_mgr._data.get("expires_at", 0),
                "token_remaining": max(0, token_mgr._data.get("expires_at", 0) - time.time()),
                "concurrent_limit": MAX_CONCURRENT,
                "concurrent_active": _active_requests,
                "cache": response_cache.stats(),
                "truncation": {
                    "enabled": ENABLE_TRUNCATE,
                    "max_history_pairs": MAX_HISTORY_PAIRS,
                    "max_assistant_chars": MAX_ASSISTANT_CHARS,
                },
                "single_flight": {
                    "enabled": ENABLE_SINGLE_FLIGHT,
                    "timeout": SINGLE_FLIGHT_TIMEOUT,
                },
                "semantic_cache": {
                    "enabled": ENABLE_SEMANTIC_CACHE,
                    "mode": "normalized_equivalence",
                },
                "features": features,
            }
            self.wfile.write(json.dumps(health).encode())
            self.wfile.flush()
            return

        if self.path == "/metrics":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            snapshot = metrics.snapshot()
            snapshot["cache"] = response_cache.stats()
            snapshot["truncation"] = {
                "enabled": ENABLE_TRUNCATE,
                "max_history_pairs": MAX_HISTORY_PAIRS,
                "max_assistant_chars": MAX_ASSISTANT_CHARS,
            }
            snapshot["single_flight"] = {
                "enabled": ENABLE_SINGLE_FLIGHT,
                "timeout": SINGLE_FLIGHT_TIMEOUT,
            }
            snapshot["semantic_cache"] = {
                "enabled": ENABLE_SEMANTIC_CACHE,
                "mode": "normalized_equivalence",
            }
            self.wfile.write(json.dumps(snapshot).encode())
            self.wfile.flush()
            return

        self._forward("GET")

    def do_POST(self):
        self._forward("POST")


def _signal_handler(signum, frame):
    sig_name = signal.Signals(signum).name
    if sig_name == "SIGHUP":
        logger.info("Received SIGHUP, reloading config...")
        _reload_config()
        global rpm_limiter
        rpm_limiter = RPMLimiter(RPM_LIMIT)
        return
    logger.info(f"Received {sig_name}, starting graceful shutdown...")
    _shutdown_event.set()


def main():
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGHUP, _signal_handler)

    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer((PROXY_HOST, PROXY_PORT), ProxyHandler)
    logger.info("Code Agent Gateway v3.0 started")
    logger.info(f"  Listen: http://{PROXY_HOST}:{PROXY_PORT}")
    logger.info(f"  Upstream: {UPSTREAM_BASE}")
    logger.info(f"  Concurrent: {MAX_CONCURRENT}, RPM limit: {RPM_LIMIT}, Upstream timeout: {UPSTREAM_TIMEOUT}s, Queue timeout: {QUEUE_TIMEOUT}s")
    logger.info(f"  Max body size: {MAX_BODY_SIZE} bytes")
    logger.info(f"  Slow request threshold: {SLOW_REQUEST_THRESHOLD}s")
    logger.info(f"  Log dir max: {LOG_DIR_MAX_BYTES} bytes")
    logger.info(f"  Graceful shutdown wait: {GRACEFUL_SHUTDOWN_WAIT}s")
    logger.info(f"  Debug body: {DEBUG_BODY}")
    logger.info(f"  Response cache: enabled={ENABLE_CACHE} semantic={ENABLE_SEMANTIC_CACHE} ttl={CACHE_TTL}s max_entries={CACHE_MAX_ENTRIES}")
    logger.info(f"  Message truncation: enabled={ENABLE_TRUNCATE} max_pairs={MAX_HISTORY_PAIRS} max_assistant_chars={MAX_ASSISTANT_CHARS}")
    logger.info(f"  Single-flight: enabled={ENABLE_SINGLE_FLIGHT} timeout={SINGLE_FLIGHT_TIMEOUT}s")

    # Start background threads
    threading.Thread(target=refresh_worker, daemon=True).start()
    threading.Thread(target=_upstream_probe_worker, daemon=True).start()

    # Run server in a thread so we can wait for shutdown signal
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()

    # Wait for shutdown signal
    _shutdown_event.wait()

    # Stop accepting new connections
    logger.info("Shutting down server...")
    server.shutdown()

    # Wait for active requests to complete
    wait_start = time.time()
    while _active_requests > 0 and (time.time() - wait_start) < GRACEFUL_SHUTDOWN_WAIT:
        time.sleep(0.1)

    if _active_requests > 0:
        logger.warning(f"Force shutdown with {_active_requests} active requests remaining")
    else:
        logger.info("All active requests completed, shutdown cleanly")

    server_thread.join(timeout=5)
    logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
