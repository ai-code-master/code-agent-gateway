"""OpenAI-compatible request body normalization."""


def estimate_tokens(body: dict) -> int:
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return 0
    total_chars = 0
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            total_chars += sum(
                len(part["text"])
                for part in content
                if isinstance(part, dict) and isinstance(part.get("text"), str)
            )
    return total_chars // 2


def dynamic_max_tokens(body: dict) -> int:
    estimate = estimate_tokens(body)
    if estimate < 2000:
        return 4096
    if estimate < 8000:
        return 8192
    if estimate < 16000:
        return 16384
    return 32768


def safe_body_preview(body: bytes, max_len: int = 500) -> str:
    return body[:max_len].decode("utf-8", errors="replace")


class RequestBodyProcessor:
    _THINKING_BUDGETS = {"low": 4000, "medium": 8000, "high": 16000}

    def __init__(self, enabled, max_pairs, max_assistant_chars, logger):
        self._enabled = enabled
        self._max_pairs = max_pairs
        self._max_assistant_chars = max_assistant_chars
        self._logger = logger

    def inject_thinking(self, body: dict) -> dict:
        if not isinstance(body, dict) or "thinking" in body:
            return body
        effort = body.get("reasoning_effort")
        if isinstance(effort, str):
            effort = effort.strip().lower()
            budget = self._THINKING_BUDGETS.get(effort, 8000)
            body["thinking"] = {"type": "enabled", "budget_tokens": budget}
            self._logger.info(
                "Injected thinking=budget_tokens:%s from reasoning_effort=%s",
                budget, effort,
            )
        return body

    def truncate_messages(self, body: dict) -> dict:
        if not self._enabled():
            return body
        messages = body.get("messages", [])
        if not isinstance(messages, list) or len(messages) <= 2:
            return body
        system_messages = [
            message for message in messages
            if isinstance(message, dict) and message.get("role") == "system"
        ]
        conversation = [
            message for message in messages
            if not (isinstance(message, dict) and message.get("role") == "system")
        ]
        call_sources = self._tool_call_sources(conversation)
        keep_count = max(self._max_pairs() * 2, 4)
        start = max(0, len(conversation) - keep_count)
        start = self._include_tool_sources(conversation, call_sources, start)
        dropped = start
        conversation = conversation[start:]
        shortened = self._shorten_assistant_messages(conversation)
        if dropped or shortened:
            self._logger.info(
                "Message truncation: dropped=%s assistant_msgs_truncated=%s final_count=%s",
                dropped, shortened, len(system_messages) + len(conversation),
            )
        body["messages"] = system_messages + conversation
        return body

    @staticmethod
    def _tool_call_sources(messages):
        sources = {}
        for index, message in enumerate(messages):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            for call in message.get("tool_calls", []):
                if isinstance(call, dict) and call.get("id"):
                    sources[call["id"]] = index
        return sources

    @staticmethod
    def _include_tool_sources(messages, sources, start):
        while True:
            required = []
            for message in messages[start:]:
                if isinstance(message, dict) and message.get("role") == "tool":
                    source = sources.get(message.get("tool_call_id"))
                    if source is not None and source < start:
                        required.append(source)
            if not required:
                return start
            start = min(required)

    def _shorten_assistant_messages(self, messages):
        count = 0
        limit = self._max_assistant_chars()
        for message in messages:
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            content = message.get("content", "")
            if isinstance(content, str) and len(content) > limit:
                message["content"] = content[:limit] + "\n\n[...truncated by proxy]"
                count += 1
        return count
