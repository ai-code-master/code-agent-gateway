"""Concurrency control and provider response forwarding."""

import json
import time

from gateway.errors import classify_error
from gateway.request_body import safe_body_preview


class ExecutionMixin:
    def _execute(self, request):
        context = self.context
        config = context.config()
        lease, queue_wait = context.admission.acquire(config["queue_timeout"])
        if not lease:
            context.metrics.record_request(
                time.time() - request.started_at, 503, queue_wait,
                provider="kimi",
            )
            return self._send_error(
                503,
                "Gateway concurrency limit exceeded, try again later",
                {"Retry-After": str(min(120, config["upstream_timeout"]))},
            )
        with lease as active:
            context.logger.info(
                "[%s] %s -> %s %s body=%sb queue_wait=%.2fs active=%s/%s",
                request.request_id, request.client_ip, request.method, request.path,
                request.body_length, queue_wait, active, config["max_concurrent"],
            )
            if request.streaming:
                return self._stream_upstream(request, queue_wait)
            return self._buffer_upstream(request, queue_wait)

    def _provider_request(self, request):
        context = self.context
        response, error = context.upstream_client.request(
            request.method, request.target_url, request.body, request.headers
        )
        if not response or response.status != 401:
            return response, error
        context.upstream_client.release(response, reusable=False)
        context.logger.warning("[%s] Upstream 401, refreshing token", request.request_id)
        if not context.token_manager.refresh():
            return None, RuntimeError("Token refresh failed")
        headers = {
            **request.headers,
            "Authorization": f"Bearer {context.token_manager.get_token()}",
        }
        return context.upstream_client.request(
            request.method, request.target_url, request.body, headers
        )

    def _stream_upstream(self, request, queue_wait):
        response, error = self._provider_request(request)
        if not response:
            return self._upstream_error(request, queue_wait, error)
        length = self._send_stream_response(
            response.status, response.getheaders(), response
        )
        latency = time.time() - request.started_at
        self.context.metrics.record_request(
            latency, response.status, queue_wait, request.model,
            provider="kimi",
        )
        self.context.logger.info(
            "[%s] %s -> %s (stream) latency=%.2fs resp=%sb model=%s",
            request.request_id, request.client_ip, response.status,
            latency, length, request.model or "-",
        )

    def _buffer_upstream(self, request, queue_wait):
        def operation():
            response, error = self._provider_request(request)
            if not response:
                return None, [], b"", error
            reusable = False
            try:
                result = (
                    response.status, response.getheaders(), response.read(), None
                )
                reusable = response.status < 500
                return result
            finally:
                self.context.upstream_client.release(response, reusable)

        try:
            result, coalesced = self.context.single_flight.do(
                request.method, request.path, request.body, operation
            )
            status, headers, body, error = result
        except Exception as caught:
            status, headers, body, error, coalesced = None, [], b"", caught, False
        if status is None:
            return self._upstream_error(request, queue_wait, error)
        latency = time.time() - request.started_at
        tokens = self._usage(body)
        config = self.context.config()
        if config["debug_body"] and body:
            self.context.logger.debug(
                "[%s] Response body preview: %s",
                request.request_id, safe_body_preview(body),
            )
        if request.body_json and status == 200:
            self.context.response_cache.put(
                request.path, request.body_json, body, headers, status
            )
        self.context.metrics.record_request(
            latency, status, queue_wait, request.model, tokens,
            provider="kimi",
        )
        suffix = " (coalesced)" if coalesced else ""
        log = self.context.logger.warning if latency > config["slow_request_threshold"] else self.context.logger.info
        log(
            "[%s] %s -> %s%s latency=%.2fs req=%sb resp=%sb model=%s",
            request.request_id, request.client_ip, status, suffix, latency,
            request.body_length, len(body), request.model or "-",
        )
        return self._send_response(status, headers, body)

    def _upstream_error(self, request, queue_wait, error):
        error_type, message = classify_error(error)
        latency = time.time() - request.started_at
        if error_type == "upstream_timeout":
            self.context.metrics.record_timeout()
        self.context.metrics.record_request(
            latency, 502, queue_wait, request.model, provider="kimi"
        )
        self.context.logger.error(
            "[%s] %s -> 502 (%s): %s",
            request.request_id, request.client_ip, error_type, message,
        )
        return self._send_error(502, f"{error_type}: {message}")

    @staticmethod
    def _usage(body):
        try:
            usage = json.loads(body).get("usage", {}) if body else {}
        except (ValueError, TypeError):
            usage = {}
        return {
            "input": usage.get("prompt_tokens", 0),
            "output": usage.get("completion_tokens", 0),
            "total": usage.get("total_tokens", 0),
        }
