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

import http.client
import json
import os
import signal
import sys
import threading
import time
import urllib.parse
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from gateway.config import GatewayConfig
from gateway.cache import ModelListCache, ResponseCache, SingleFlight
from gateway.errors import classify_error
from gateway.limits import RPMLimiter
from gateway.logging_setup import build_logger, install_exception_hook
from gateway.metrics import Metrics
from gateway.request_body import (
    RequestBodyProcessor,
    dynamic_max_tokens as _dynamic_max_tokens,
    estimate_tokens as _estimate_tokens,
    safe_body_preview as _safe_body_preview,
)

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

logger = build_logger(
    LOG_DIR, LOG_FILE, LOG_MAX_BYTES, LOG_BACKUP_COUNT, LOG_DIR_MAX_BYTES
)
install_exception_hook(logger)

metrics = Metrics(SLOW_REQUEST_THRESHOLD)

rpm_limiter = RPMLimiter(RPM_LIMIT)

# ==================== Concurrency Control ====================
kimi_semaphore = threading.Semaphore(MAX_CONCURRENT)
_active_requests_lock = threading.Lock()
_active_requests = 0
_shutdown_event = threading.Event()

model_cache = ModelListCache(UPSTREAM_BASE, logger, ttl=300)
response_cache = ResponseCache(
    ttl=CACHE_TTL,
    max_entries=CACHE_MAX_ENTRIES,
    enabled=lambda: ENABLE_CACHE,
    semantic_enabled=lambda: ENABLE_SEMANTIC_CACHE,
)
single_flight = SingleFlight(
    enabled=lambda: ENABLE_SINGLE_FLIGHT,
    timeout=lambda: SINGLE_FLIGHT_TIMEOUT,
)
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


body_processor = RequestBodyProcessor(
    enabled=lambda: ENABLE_TRUNCATE,
    max_pairs=lambda: MAX_HISTORY_PAIRS,
    max_assistant_chars=lambda: MAX_ASSISTANT_CHARS,
    logger=logger,
)

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
                req_json = body_processor.truncate_messages(req_json)
                # Dynamic max_tokens based on estimated input size
                if "max_tokens" not in req_json:
                    suggested = _dynamic_max_tokens(req_json)
                    req_json["max_tokens"] = suggested
                    logger.info(f"[{request_id}] Dynamic max_tokens={suggested} (est_input ~{_estimate_tokens(req_json)} tokens)")
                else:
                    logger.debug(f"[{request_id}] Preserving max_tokens={req_json['max_tokens']}")
                req_json = body_processor.inject_thinking(req_json)
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
