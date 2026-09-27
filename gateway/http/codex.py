"""OpenAI-compatible HTTP delivery for the in-process Codex provider."""

import json
import time

from codex_bridge.responses import stream_chunk, stream_common, tool_call_delta
from gateway.provider import AppServerError


class CodexMixin:
    def _forward_codex(self, body, started_at, request_id, client_ip):
        headers = {key.lower(): value for key, value in self.headers.items()}
        try:
            if body.get("stream"):
                return self._stream_codex(body, headers, started_at, request_id)
            payload = self.context.codex_provider.complete(body, headers)
            latency = time.time() - started_at
            self.context.metrics.record_request(
                latency, 200, model=body.get("model", "")
            )
            self.context.logger.info(
                "[%s] %s -> 200 (codex) latency=%.2fs model=%s",
                request_id, client_ip, latency, body.get("model", "-"),
            )
            return self._json(200, payload)
        except (ValueError, json.JSONDecodeError) as error:
            return self._json(400, {
                "error": {"message": str(error), "type": "invalid_request_error"}
            })
        except AppServerError as error:
            self.context.metrics.record_request(
                time.time() - started_at, 502, model=body.get("model", "")
            )
            return self._json(502, {
                "error": {"message": str(error), "type": "codex_backend_error"}
            })
        except Exception as error:
            self.context.logger.error(
                "[%s] Codex provider failed: %s", request_id, error, exc_info=True
            )
            return self._json(500, {
                "error": {"message": "Codex provider failed", "type": "internal_error"}
            })

    def _stream_codex(self, body, headers, started_at, request_id):
        model = body.get("model") or "codex-sol"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        common = stream_common(model)
        self._write_sse(stream_chunk(common, {"role": "assistant", "content": ""}))
        try:
            result, _ = self.context.codex_provider.run(
                body, headers,
                on_delta=lambda text: self._write_sse(
                    stream_chunk(common, {"content": text})
                ),
            )
            if result.tool_call:
                self._write_sse(stream_chunk(common, tool_call_delta(result.tool_call)))
                finish = "tool_calls"
            else:
                finish = "stop"
            self._write_sse(stream_chunk(common, {}, finish))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.context.metrics.record_request(
                time.time() - started_at, 200, model=model
            )
        except (BrokenPipeError, ConnectionResetError):
            self.context.metrics.record_client_reset()
        except AppServerError as error:
            self._write_sse({
                "error": {"message": str(error), "type": "codex_backend_error"}
            })
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        self.context.logger.info(
            "[%s] Codex stream finished latency=%.2fs model=%s",
            request_id, time.time() - started_at, model,
        )

    def _write_sse(self, payload):
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self.wfile.write(f"data: {data}\n\n".encode())
        self.wfile.flush()
