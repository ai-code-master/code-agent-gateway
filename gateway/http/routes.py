"""Health and metrics routes."""

import json


class RouteMixin:
    upstream_health = None
    token_manager = None
    response_cache = None
    current_config = None
    active_requests = None

    def do_GET(self):
        if self.path == "/":
            return self._root()
        if self.path == "/healthz":
            return self._health()
        if self.path == "/metrics":
            return self._metrics()
        return self._forward("GET")

    def do_POST(self):
        return self._forward("POST")

    def _root(self):
        self._json(200, {
            "name": "code-agent-gateway",
            "version": "3.2",
            "status": "ok",
            "health": "/healthz",
            "models": "/v1/models",
        })

    def _health(self):
        upstream_ok = self.upstream_health.is_healthy()
        ready = self.upstream_health.is_ready()
        config = self.current_config()
        payload = {
            "status": "ok" if upstream_ok else ("degraded" if ready else "unavailable"),
            "ready": ready,
            "upstream_healthy": upstream_ok,
            "providers": self.upstream_health.provider_status(),
            "version": "3.2",
            **self.token_manager.status(),
            "concurrent_limit": config["max_concurrent"],
            "concurrent_active": self.active_requests(),
            "cache": self.response_cache.stats(),
            "truncation": config["truncation"],
            "single_flight": config["single_flight"],
            "equivalence_cache": config["equivalence_cache"],
            "features": self._features(config),
        }
        payload["codex_pool"] = self.codex_provider.status()
        payload["kimi_transport"] = self.context.upstream_client.status()
        payload["model_cache"] = self.context.model_cache.status()
        self._json(200 if ready else 503, payload)

    def _metrics(self):
        config = self.current_config()
        payload = self.metrics.snapshot()
        payload.update({
            "cache": self.response_cache.stats(),
            "truncation": config["truncation"],
            "single_flight": config["single_flight"],
            "equivalence_cache": config["equivalence_cache"],
        })
        self._json(200, payload)

    def _json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.wfile.flush()

    @staticmethod
    def _features(config):
        features = [
            "connection_close", "backoff_retry", "auto_refresh_401",
            "full_xmsh_headers", "thinking_injection", "metrics",
            "structured_access_log", "client_reset_guard", "body_size_guard",
            "upstream_health_probe", "model_list_cache", "dynamic_max_tokens",
            "runtime_config_reload", "token_tracking", "true_streaming",
            "unified_provider_routing", "codex_app_server",
        ]
        flags = (
            (config["debug_body"], "debug_body"),
            (config["cache"]["enabled"], "response_cache"),
            (config["truncation"]["enabled"], "message_truncation"),
            (config["single_flight"]["enabled"], "single_flight"),
            (config["equivalence_cache"]["enabled"], "equivalence_cache"),
        )
        features.extend(name for enabled, name in flags if enabled)
        return features
