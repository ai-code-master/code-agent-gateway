import json
import time
import uuid


def completion(result, model):
    message = {"role": "assistant", "content": result.content or None}
    finish = "stop"
    if result.tool_call:
        message["tool_calls"] = [_tool_call(result.tool_call)]
        finish = "tool_calls"
    return {
        "id": _id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def stream_chunks(result, model):
    common = stream_common(model)
    yield _chunk(common, {"role": "assistant", "content": ""}, None)
    if result.tool_call:
        delta = {"tool_calls": [{"index": 0, **_tool_call(result.tool_call)}]}
        yield _chunk(common, delta, None)
        yield _chunk(common, {}, "tool_calls")
    else:
        yield _chunk(common, {"content": result.content}, None)
        yield _chunk(common, {}, "stop")


def stream_common(model):
    return {
        "id": _id(),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
    }


def stream_chunk(common, delta, finish=None):
    return _chunk(common, delta, finish)


def tool_call_delta(call):
    return {"tool_calls": [{"index": 0, **_tool_call(call)}]}


def _tool_call(call):
    arguments = call.get("arguments", {})
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
    return {
        "id": call.get("id") or f"call_{uuid.uuid4().hex}",
        "type": "function",
        "function": {"name": call["name"], "arguments": arguments},
    }


def _chunk(common, delta, finish):
    data = dict(common)
    data["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish}]
    return data


def _id():
    return f"chatcmpl-codex-{uuid.uuid4().hex}"
