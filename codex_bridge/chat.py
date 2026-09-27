import json
import os
import re
from pathlib import Path


def _text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)
    parts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in ("text", "input_text"):
            parts.append(item.get("text", ""))
        elif item.get("type") in ("image_url", "input_image"):
            parts.append("[图像输入由客户端提供]")
    return "\n".join(parts)


def build_prompt(messages):
    blocks = [
        "你是通过 Code Agent Gateway 调用的 Codex 执行引擎。",
        "完整遵循下面的对话与系统要求。需要外部操作时，优先调用 client_tools 命名空间工具。",
        "这是截至当前请求的完整对话记录；已有 tool 消息就是已完成工具调用的真实结果，不要重复同一调用。",
        "不要解释桥接机制；直接完成用户任务。",
    ]
    for message in messages or []:
        role = message.get("role", "unknown")
        body = _text(message.get("content"))
        meta = []
        if message.get("name"):
            meta.append(f'name={json.dumps(message["name"], ensure_ascii=False)}')
        if message.get("tool_call_id"):
            meta.append(f'tool_call_id={json.dumps(message["tool_call_id"])}')
        attr = (" " + " ".join(meta)) if meta else ""
        blocks.append(f'<message role="{role}"{attr}>\n{body}\n</message>')
        calls = message.get("tool_calls") or []
        if calls:
            blocks.append("<assistant_tool_calls>\n" + json.dumps(
                calls, ensure_ascii=False, separators=(",", ":")
            ) + "\n</assistant_tool_calls>")
    return "\n\n".join(blocks)


def dynamic_tools(tools):
    converted = []
    for entry in tools or []:
        fn = entry.get("function", {}) if isinstance(entry, dict) else {}
        name = fn.get("name")
        if not name:
            continue
        converted.append({
            "type": "function",
            "name": name,
            "description": fn.get("description") or f"Client tool {name}",
            "inputSchema": fn.get("parameters") or {"type": "object"},
        })
    if not converted:
        return None
    return [{
        "type": "namespace",
        "name": "client_tools",
        "description": "Tools supplied by the connected OpenAI-compatible client.",
        "tools": converted,
    }]


def detect_cwd(messages, headers):
    for key in ("x-workspace-root", "x-working-directory", "x-cwd"):
        candidate = headers.get(key)
        if candidate and _valid_dir(candidate):
            return candidate
    joined = "\n".join(
        _text(m.get("content")) for m in (messages or []) if m.get("role") == "system"
    )
    patterns = (
        r"<cwd>\s*([^<\n]+)",
        r"(?:Working directory|Current working directory|工作目录)\s*[:：]\s*([^\n]+)",
        r'"cwd"\s*:\s*"([^"]+)"',
    )
    for pattern in patterns:
        match = re.search(pattern, joined, re.IGNORECASE)
        if match and _valid_dir(match.group(1).strip()):
            return match.group(1).strip()
    fallback = os.environ.get("CAG_CODEX_CWD", str(Path.home()))
    return fallback if _valid_dir(fallback) else str(Path.home())


def _valid_dir(value):
    try:
        path = Path(value).expanduser()
        return path.is_absolute() and path.is_dir()
    except (OSError, TypeError):
        return False
