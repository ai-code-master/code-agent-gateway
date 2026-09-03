#!/usr/bin/env python3
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from appserver import AppServerError, run_codex
from chat import build_prompt, detect_cwd, dynamic_tools
from responses import completion, stream_chunk, stream_common, tool_call_delta


HOST = os.environ.get("CODEX_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("CODEX_BRIDGE_PORT", "8766"))
MODEL_MAP = {
    "codex-local": ("gpt-5.6-sol", "high"),
    "codex-spark": ("gpt-5.3-codex-spark", "high"),
    "codex-sol": ("gpt-5.6-sol", "high"),
    "codex-terra": ("gpt-5.6-terra", "medium"),
    "codex-luna": ("gpt-5.6-luna", "low"),
}


def _normalize_model(model_name: str | None) -> str | None:
    if not model_name:
        return None
    if model_name.startswith("custom-local:"):
        return model_name[len("custom-local:"):]
    return model_name
PUBLIC_MODELS = ("codex-spark", "codex-sol", "codex-terra", "codex-luna")


class Handler(BaseHTTPRequestHandler):
    server_version = "WorkBuddyCodexBridge/0.2"

    def do_GET(self):
        if self.path.rstrip("/") == "/healthz":
            return self._json(200, {"ok": True, "backend": "codex-app-server"})
        if self.path.rstrip("/").endswith("/models"):
            return self._json(200, {
                "object": "list",
                "data": [
                    {"id": name, "object": "model", "owned_by": "openai"}
                    for name in PUBLIC_MODELS
                ],
            })
        self._json(404, {"error": {"message": "Not found"}})

    def do_POST(self):
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._json(404, {"error": {"message": "Not found"}})
        try:
            length = int(self.headers.get("content-length", "0"))
            if length <= 0 or length > 20 * 1024 * 1024:
                raise ValueError("Invalid request size")
            body = json.loads(self.rfile.read(length))
            messages = body.get("messages") or []
            cwd = detect_cwd(messages, {k.lower(): v for k, v in self.headers.items()})
            tools = dynamic_tools(body.get("tools"))
            requested_model = body.get("model") or "codex-sol"
            normalized_model = _normalize_model(requested_model)
            codex_model, default_effort = MODEL_MAP.get(
                normalized_model, ("gpt-5.6-sol", "high")
            )
            effort = body.get("reasoning_effort") or default_effort
            self._log_meta(body, cwd, tools, codex_model, effort)
            if body.get("stream"):
                return self._stream_request(
                    build_prompt(messages), cwd, tools, effort,
                    codex_model, requested_model,
                )
            result = run_codex(
                build_prompt(messages), cwd, tools=tools,
                effort=effort, model=codex_model,
            )
            self._json(200, completion(result, requested_model))
        except (ValueError, json.JSONDecodeError) as exc:
            self._json(400, {"error": {"message": str(exc), "type": "invalid_request_error"}})
        except AppServerError as exc:
            self._json(502, {"error": {"message": str(exc), "type": "codex_backend_error"}})
        except Exception as exc:
            print(f"unexpected error: {exc}", file=sys.stderr)
            self._json(500, {"error": {"message": "Bridge internal error"}})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def _stream_request(self, prompt, cwd, tools, effort, codex_model, model):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        common = stream_common(model)
        self._write_sse(stream_chunk(common, {"role": "assistant", "content": ""}))
        try:
            result = run_codex(
                prompt, cwd, tools=tools, effort=effort, model=codex_model,
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
        except (BrokenPipeError, ConnectionResetError):
            return
        except AppServerError as exc:
            self._write_sse({
                "error": {"message": str(exc), "type": "codex_backend_error"}
            })
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    def _write_sse(self, payload):
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self.wfile.write(f"data: {data}\n\n".encode())
        self.wfile.flush()

    def _json(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _log_meta(self, body, cwd, tools, codex_model, effort):
        roles = [m.get("role") for m in body.get("messages", [])]
        count = sum(len(x.get("tools", [])) for x in (tools or []))
        print(
            f"request model={body.get('model')} codex={codex_model} effort={effort} "
            f"roles={roles} tools={count} cwd={cwd}"
        )

    def log_message(self, fmt, *args):
        print(f"{self.client_address[0]} {fmt % args}")


if __name__ == "__main__":
    print(f"WorkBuddy Codex Bridge listening on http://{HOST}:{PORT}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
