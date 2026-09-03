import json
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class RunResult:
    content: str = ""
    tool_call: Optional[dict] = None


class AppServerError(RuntimeError):
    pass


def _reader(stream, output):
    for line in stream:
        output.put(line)
    output.put(None)


def _send(proc, payload):
    proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
    proc.stdin.flush()


def run_codex(prompt, cwd, tools=None, effort=None, model=None, timeout=900):
    proc = subprocess.Popen(
        ["codex", "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    output = queue.Queue()
    errors = queue.Queue()
    threading.Thread(target=_reader, args=(proc.stdout, output), daemon=True).start()
    threading.Thread(target=_reader, args=(proc.stderr, errors), daemon=True).start()
    try:
        _handshake(proc)
        thread_id = _start_thread(proc, output, cwd, tools, model, timeout)
        params = {
            "threadId": thread_id,
            "input": [{"type": "text", "text": prompt}],
        }
        if effort in {"minimal", "low", "medium", "high", "xhigh", "max"}:
            params["effort"] = effort
        _send(proc, {"method": "turn/start", "id": 2, "params": params})
        return _await_turn(output, timeout)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def _handshake(proc):
    _send(proc, {
        "method": "initialize",
        "id": 0,
        "params": {
            "clientInfo": {
                "name": "workbuddy_codex_bridge",
                "title": "WorkBuddy Codex Bridge",
                "version": "0.2.0",
            },
            "capabilities": {"experimentalApi": True},
        },
    })
    _send(proc, {"method": "initialized", "params": {}})


def _start_thread(proc, output, cwd, tools, model, timeout):
    params = {
        "cwd": cwd,
        "approvalPolicy": "never",
        "sandbox": "read-only",
        "ephemeral": True,
        "serviceName": "workbuddy_codex_bridge",
    }
    if tools:
        params["dynamicTools"] = tools
    if model:
        params["model"] = model
    _send(proc, {"method": "thread/start", "id": 1, "params": params})
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        msg = _next(output, deadline)
        if msg.get("id") == 1:
            _raise_error(msg)
            thread_id = msg.get("result", {}).get("thread", {}).get("id")
            if not thread_id:
                raise AppServerError("thread/start did not return a thread id")
            return thread_id
    raise AppServerError("thread/start timed out")


def _await_turn(output, timeout):
    deadline = time.monotonic() + timeout
    final_messages = []
    unknown_messages = []
    while time.monotonic() < deadline:
        msg = _next(output, deadline)
        if msg.get("id") == 2:
            _raise_error(msg)
        if msg.get("method") == "item/tool/call":
            params = msg.get("params", {})
            return RunResult(tool_call={
                "id": params.get("callId"),
                "name": params.get("tool"),
                "arguments": params.get("arguments", {}),
            })
        if msg.get("method") == "item/completed":
            item = msg.get("params", {}).get("item", {})
            if item.get("type") == "agentMessage":
                target = final_messages if item.get("phase") == "final_answer" else unknown_messages
                target.append(item.get("text", ""))
        if msg.get("method") == "turn/completed":
            turn = msg.get("params", {}).get("turn", {})
            if turn.get("status") == "failed":
                raise AppServerError(str(turn.get("error") or "Codex turn failed"))
            content = "\n".join(x for x in (final_messages or unknown_messages) if x)
            return RunResult(content=content)
    raise AppServerError("Codex turn timed out")


def _next(output, deadline):
    wait = max(0.1, min(1.0, deadline - time.monotonic()))
    try:
        line = output.get(timeout=wait)
    except queue.Empty:
        return {}
    if line is None:
        raise AppServerError("Codex app-server exited unexpectedly")
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {}


def _raise_error(message):
    if message.get("error"):
        raise AppServerError(message["error"].get("message", str(message["error"])))
