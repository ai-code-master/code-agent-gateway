"""JSON-RPC helpers for the Codex App Server protocol."""

import json
import queue
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class RunResult:
    content: str = ""
    tool_call: Optional[dict] = None


class AppServerError(RuntimeError):
    pass


def read_lines(stream, output):
    for line in stream:
        output.put(line)
    output.put(None)


def send(proc, payload):
    proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
    proc.stdin.flush()


def start_thread(proc, output, cwd, tools, model, timeout):
    params = {
        "cwd": cwd,
        "ephemeral": True, "serviceName": "code-agent-gateway",
    }
    if tools:
        params["dynamicTools"] = tools
    if model:
        params["model"] = model
    send(proc, {"method": "thread/start", "id": 1, "params": params})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = next_message(output, deadline)
        if message.get("id") == 1:
            raise_rpc_error(message)
            thread_id = message.get("result", {}).get("thread", {}).get("id")
            if thread_id:
                return thread_id
            raise AppServerError("thread/start did not return a thread id")
    raise AppServerError("thread/start timed out")


def list_models(proc, output, timeout):
    deadline = time.monotonic() + timeout
    request_id, cursor, models = 100, None, []
    while time.monotonic() < deadline:
        params = {"includeHidden": False}
        if cursor:
            params["cursor"] = cursor
        send(proc, {
            "method": "model/list", "id": request_id, "params": params,
        })
        while time.monotonic() < deadline:
            message = next_message(output, deadline)
            if message.get("id") != request_id:
                continue
            raise_rpc_error(message)
            result = message.get("result", {})
            models.extend(
                item["id"] for item in result.get("data", [])
                if isinstance(item, dict) and item.get("id")
            )
            cursor = result.get("nextCursor")
            break
        if not cursor:
            return list(dict.fromkeys(models))
        request_id += 1
    raise AppServerError("model/list timed out")


def await_turn(output, timeout, on_delta=None):
    deadline = time.monotonic() + timeout
    final_messages, unknown_messages, phases, streamed_ids = [], [], {}, set()
    while time.monotonic() < deadline:
        message = next_message(output, deadline)
        if message.get("id") == 2:
            raise_rpc_error(message)
        method, params = message.get("method"), message.get("params", {})
        if method == "item/tool/call":
            return RunResult(tool_call={
                "id": params.get("callId"), "name": params.get("tool"),
                "arguments": params.get("arguments", {}),
            })
        item = params.get("item", {})
        if method == "item/started" and item.get("type") == "agentMessage":
            phases[item.get("id")] = item.get("phase")
        if method == "item/agentMessage/delta":
            item_id, delta = params.get("itemId"), params.get("delta", "")
            if on_delta and delta and phases.get(item_id) == "final_answer":
                on_delta(delta)
                streamed_ids.add(item_id)
        if method == "item/completed" and item.get("type") == "agentMessage":
            target = final_messages if item.get("phase") == "final_answer" else unknown_messages
            target.append(item.get("text", ""))
            if on_delta and item.get("text") and item.get("phase") == "final_answer":
                if item.get("id") not in streamed_ids:
                    on_delta(item["text"])
        if method == "turn/completed":
            turn = params.get("turn", {})
            if turn.get("status") == "failed":
                raise AppServerError(str(turn.get("error") or "Codex turn failed"))
            content = "\n".join(x for x in (final_messages or unknown_messages) if x)
            return RunResult(content=content)
    raise AppServerError("Codex turn timed out")


def next_message(output, deadline):
    try:
        line = output.get(timeout=max(0.1, min(1.0, deadline - time.monotonic())))
    except queue.Empty:
        return {}
    if line is None:
        raise AppServerError("Codex app-server exited unexpectedly")
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {}


def raise_rpc_error(message):
    if message.get("error"):
        raise AppServerError(message["error"].get("message", str(message["error"])))
