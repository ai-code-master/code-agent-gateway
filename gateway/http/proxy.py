"""Request validation, normalization, and cache lookup."""

import json
import time
import uuid

from gateway.request_body import dynamic_max_tokens, estimate_tokens, safe_body_preview

from .context import PreparedRequest


class ProxyMixin:
    context = None

    def _forward(self, method):
        context = self.context
        config = context.config()
        started_at = time.time()
        client_ip = self._client_ip()
        request_id = self.headers.get("x-request-id") or f"cag-{uuid.uuid4().hex[:12]}"
        if self.path == "/admin/config":
            return self._json(200, config)
        if self.path == "/admin/reload":
            return self._json(200, {
                "status": "reloaded", "config": context.reload()
            })
        limiter = context.rate_limiter()
        if not limiter.allow():
            current = limiter.current()
            context.metrics.record_request(time.time() - started_at, 429)
            context.logger.warning(
                "[%s] %s -> 429 (RPM limit %s/%s)",
                request_id, client_ip, current, config["rpm_limit"],
            )
            return self._send_error(
                429, f"Rate limit exceeded: {current} requests in the last minute"
            )
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > config["max_body_size"]:
            context.metrics.record_request(time.time() - started_at, 413)
            return self._send_error(
                413, f"Request body too large: {content_length} bytes"
            )
        body = self.rfile.read(content_length) if content_length > 0 else b""
        path = self._normalized_path()
        if method == "GET" and path == "/v1/models":
            return self._send_models(context.token_manager.get_token())
        try:
            raw_json = json.loads(body) if body else {}
        except (ValueError, TypeError):
            raw_json = {}
        if (
            method == "POST" and path == "/v1/responses"
            and isinstance(raw_json, dict)
        ):
            return self._forward_responses(
                raw_json, started_at, request_id, client_ip
            )
        if (
            method == "POST"
            and path in ("/v1/chat/completions", "/chat/completions")
            and isinstance(raw_json, dict)
            and context.codex_provider.handles(raw_json.get("model"))
        ):
            return self._forward_codex(
                raw_json, started_at, request_id, client_ip
            )
        token = context.token_manager.get_token()
        if not token:
            context.metrics.record_request(time.time() - started_at)
            context.logger.warning("[%s] %s -> 401 (no token)", request_id, client_ip)
            return self._send_error(401, "No Kimi Code token available")
        if config["debug_body"] and body:
            context.logger.debug(
                "[%s] Request body preview: %s", request_id, safe_body_preview(body)
            )
        target_url = self._target_url(config["upstream_base"], path)
        body, body_json, model = self._prepare_body(body, request_id, config)
        streaming = bool(body_json.get("stream")) if isinstance(body_json, dict) else False
        if method == "POST" and body_json:
            cached = context.response_cache.get_equivalent(path, body_json)
            if cached:
                return self._cached_response(
                    cached, started_at, request_id, client_ip, model
                )
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "KimiCLI/1.5",
            "Accept": "text/event-stream" if streaming else "application/json",
            "X-Request-Id": request_id,
            **context.token_manager.get_headers(),
        }
        for key in ("openai-beta", "anthropic-version"):
            if key in self.headers:
                headers[key] = self.headers[key]
        request = PreparedRequest(
            method, path, target_url, body, body_json, len(body), model,
            streaming, headers, started_at, client_ip, request_id,
        )
        return self._execute(request)

    def _send_models(self, token):
        payload = {"object": "list", "data": []}
        if token:
            cached = self.context.model_cache.get(token)
            if cached:
                try:
                    payload = json.loads(cached)
                except (ValueError, TypeError):
                    payload = {"object": "list", "data": []}
        records = payload.setdefault("data", [])
        existing = {
            item.get("id") for item in records if isinstance(item, dict)
        }
        records.extend(
            item for item in self.context.codex_provider.model_records()
            if item["id"] not in existing
        )
        return self._json(200, payload)

    def _prepare_body(self, body, request_id, config):
        model = ""
        body_json = {}
        try:
            body_json = json.loads(body) if body else {}
            if isinstance(body_json, dict):
                model = body_json.get("model", "")
                body_json = self.context.body_processor.truncate_messages(body_json)
                if "max_tokens" not in body_json:
                    suggested = dynamic_max_tokens(body_json)
                    body_json["max_tokens"] = suggested
                    self.context.logger.info(
                        "[%s] Dynamic max_tokens=%s (est_input ~%s tokens)",
                        request_id, suggested, estimate_tokens(body_json),
                    )
                body_json = self.context.body_processor.inject_thinking(body_json)
                body = json.dumps(body_json).encode()
        except Exception as error:
            self.context.logger.debug("[%s] Body enrichment skipped: %s", request_id, error)
            if config["debug_body"] and body:
                self.context.logger.debug(
                    "[%s] Raw body preview: %s", request_id, safe_body_preview(body)
                )
        return body, body_json, model

    def _cached_response(self, cached, started_at, request_id, client_ip, model):
        data, headers = cached
        self.send_response(200)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("X-Cache", "HIT")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            self.context.logger.debug("Client disconnected during cached response write")
        latency = time.time() - started_at
        self.context.metrics.record_request(latency, 200, model=model)
        self.context.logger.info(
            "[%s] %s -> 200 (cache_hit) latency=%.2fs model=%s",
            request_id, client_ip, latency, model or "-",
        )

    def _normalized_path(self):
        path = self.path[4:] if self.path.startswith("/api/") else self.path
        return "/v1/models" if path.startswith("/v1/models/") else path

    @staticmethod
    def _target_url(upstream_base, path):
        if path.startswith("/v1/"):
            return f"{upstream_base}{path}"
        if path == "/chat/completions":
            return f"{upstream_base}/v1/chat/completions"
        if path == "/models":
            return f"{upstream_base}/v1/models"
        return f"{upstream_base}/v1{path}"
