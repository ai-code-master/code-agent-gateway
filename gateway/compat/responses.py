"""Conversions between OpenAI Responses and Chat Completions payloads."""

import time
import uuid


def responses_to_chat(body):
    messages = []
    if body.get("instructions"):
        messages.append({"role": "system", "content": body["instructions"]})
    input_value = body.get("input", "")
    if isinstance(input_value, str):
        messages.append({"role": "user", "content": input_value})
    elif isinstance(input_value, list):
        messages.extend(_input_messages(input_value))
    chat = {
        "model": body.get("model") or "codex-sol",
        "messages": messages,
        "stream": bool(body.get("stream")),
    }
    for key in ("temperature", "top_p", "tool_choice"):
        if key in body:
            chat[key] = body[key]
    if "max_output_tokens" in body:
        chat["max_tokens"] = body["max_output_tokens"]
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        chat["reasoning_effort"] = reasoning["effort"]
    tools = [_response_tool(item) for item in body.get("tools", [])]
    if tools:
        chat["tools"] = [item for item in tools if item]
    return chat


def chat_to_response(chat_payload, request_body, response_id=None):
    response_id = response_id or f"resp_{uuid.uuid4().hex}"
    choice = (chat_payload.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    output = []
    if message.get("content") is not None:
        output.append(message_item(message.get("content", "")))
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        call_id = call.get("id") or f"call_{uuid.uuid4().hex}"
        output.append({
            "type": "function_call",
            "id": f"fc_{uuid.uuid4().hex}",
            "call_id": call_id,
            "name": function.get("name", ""),
            "arguments": function.get("arguments", "{}"),
            "status": "completed",
        })
    usage = chat_payload.get("usage") or {}
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": request_body.get("model") or "codex-sol",
        "output": output,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
        },
        "error": None,
        "incomplete_details": None,
    }


def message_item(text, item_id=None, status="completed"):
    return {
        "type": "message",
        "id": item_id or f"msg_{uuid.uuid4().hex}",
        "status": status,
        "role": "assistant",
        "content": [{
            "type": "output_text", "text": text, "annotations": [],
        }],
    }


def _input_messages(items):
    messages = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "function_call_output":
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id", ""),
                "content": item.get("output", ""),
            })
        elif item.get("type") in (None, "message") or item.get("role"):
            messages.append({
                "role": item.get("role", "user"),
                "content": _content(item.get("content", "")),
            })
    return messages


def _content(content):
    if not isinstance(content, list):
        return content
    converted = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") in ("input_text", "output_text", "text"):
            converted.append({"type": "text", "text": part.get("text", "")})
        elif part.get("type") in ("input_image", "image_url"):
            converted.append({
                "type": "image_url",
                "image_url": part.get("image_url") or part.get("url"),
            })
    return converted


def _response_tool(item):
    if not isinstance(item, dict) or item.get("type") != "function":
        return None
    return {
        "type": "function",
        "function": {
            "name": item.get("name", ""),
            "description": item.get("description", ""),
            "parameters": item.get("parameters") or {"type": "object"},
        },
    }
