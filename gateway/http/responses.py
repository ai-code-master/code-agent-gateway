"""OpenAI Responses API delivery for local Codex subscriptions."""

import json
import time
import uuid

from codex_bridge.responses import completion
from gateway.compat import chat_to_response, message_item, responses_to_chat
from gateway.provider import AppServerError


class ResponsesMixin:
    def _forward_responses(self, body, started_at, request_id, client_ip):
        chat = responses_to_chat(body)
        if not self.context.codex_provider.handles(chat["model"]):
            return self._json(400, {"error": {
                "message": "/v1/responses currently requires a codex model",
                "type": "invalid_request_error",
            }})
        config = self.context.config()
        lease, queue_wait = self.context.admission.acquire(config["queue_timeout"])
        if not lease:
            self.context.metrics.record_request(
                time.time() - started_at, 503, queue_wait,
                model=chat["model"], provider="codex",
            )
            return self._send_error(
                503, "Gateway concurrency limit exceeded",
                {"Retry-After": str(min(120, config["upstream_timeout"]))},
            )
        with lease:
            return self._run_response_request(
                chat, body, started_at, request_id, client_ip, queue_wait
            )

    def _run_response_request(
        self, chat, body, started_at, request_id, client_ip, queue_wait
    ):
        headers = {key.lower(): value for key, value in self.headers.items()}
        try:
            if chat["stream"]:
                return self._stream_response_api(
                    chat, body, headers, started_at, request_id, queue_wait
                )
            result = self.context.codex_provider.complete(chat, headers)
            payload = chat_to_response(result, body)
            latency = time.time() - started_at
            self.context.metrics.record_request(
                latency, 200, queue_wait, model=chat["model"],
                provider="codex",
            )
            self.context.logger.info(
                "[%s] %s -> 200 (responses) latency=%.2fs model=%s",
                request_id, client_ip, latency, chat["model"],
            )
            return self._json(200, payload)
        except (ValueError, json.JSONDecodeError) as error:
            return self._json(400, {"error": {
                "message": str(error), "type": "invalid_request_error",
            }})
        except AppServerError as error:
            self.context.metrics.record_request(
                time.time() - started_at, 502, queue_wait,
                model=chat["model"], provider="codex",
            )
            return self._json(502, {"error": {
                "message": str(error), "type": "codex_backend_error",
            }})

    def _stream_response_api(
        self, chat, original, headers, started_at, request_id, queue_wait
    ):
        response_id = f"resp_{uuid.uuid4().hex}"
        item_id = f"msg_{uuid.uuid4().hex}"
        model = chat["model"]
        chunks = []
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        created = chat_to_response(
            {"choices": [{"message": {"content": ""}}]}, original, response_id
        )
        created["status"], created["output"] = "in_progress", []
        self._response_event("response.created", {"response": created})
        self._response_event("response.output_item.added", {
            "output_index": 0,
            "item": message_item("", item_id, "in_progress"),
        })

        def on_delta(text):
            chunks.append(text)
            self._response_event("response.output_text.delta", {
                "item_id": item_id, "output_index": 0,
                "content_index": 0, "delta": text,
            })

        try:
            result, _ = self.context.codex_provider.run(
                chat, headers, on_delta=on_delta
            )
            final_text = result.content or "".join(chunks)
            self._response_event("response.output_text.done", {
                "item_id": item_id, "output_index": 0,
                "content_index": 0, "text": final_text,
            })
            payload = chat_to_response(
                completion(result, model), original, response_id
            )
            self._response_event("response.completed", {"response": payload})
            self.context.metrics.record_request(
                time.time() - started_at, 200, queue_wait, model=model,
                provider="codex",
            )
        except AppServerError as error:
            self.context.metrics.record_request(
                time.time() - started_at, 502, queue_wait, model=model,
                provider="codex",
            )
            self._response_event("response.failed", {"response": {
                "id": response_id, "object": "response", "status": "failed",
                "error": {"message": str(error), "type": "codex_backend_error"},
            }})

    def _response_event(self, event, payload):
        data = {"type": event, **payload}
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        self.wfile.write(f"event: {event}\ndata: {encoded}\n\n".encode())
        self.wfile.flush()
