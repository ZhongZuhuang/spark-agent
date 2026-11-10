from __future__ import annotations

import json
from typing import Any

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)


def _part_text(part: Any) -> str:
    if isinstance(part, (TextPart, ThinkingPart, UserPromptPart, ToolReturnPart)):
        return str(getattr(part, "content", "") or "")
    if isinstance(part, ToolCallPart):
        return f"{part.tool_name}({_json(part.args)})"
    return ""


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def estimate_tokens(messages: list[ModelMessage], extra_text: str = "") -> int:
    try:
        serialized = _HISTORY_ADAPTER.dump_json(messages).decode("utf-8", errors="replace")
    except Exception:
        serialized = str(messages)
    extra = extra_text if isinstance(extra_text, str) else str(extra_text)
    return max(1, (len(serialized) + len(extra)) // 4)


def _user_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        bits: list[str] = []
        for item in content:
            text = getattr(item, "text", None)
            if isinstance(text, str) and text:
                bits.append(text)
            elif isinstance(item, str):
                bits.append(item)
            elif getattr(item, "is_image", False) or type(item).__name__ == "BinaryContent":
                bits.append("[image]")
        return " ".join(bits)
    return str(content or "")


def summarize_history(messages: list[ModelMessage], limit: int = 6000) -> str:
    users: list[str] = []
    answers: list[str] = []
    tools: list[str] = []
    for message in messages:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart):
                    text = _user_text(part.content).strip()
                    if text:
                        users.append(text)
                elif isinstance(part, ToolReturnPart):
                    tools.append(f"{part.tool_name}: {str(part.content)[:180]}")
        elif isinstance(message, ModelResponse):
            for part in message.parts:
                if isinstance(part, TextPart):
                    text = str(part.content or "").strip()
                    if text:
                        answers.append(text)
                elif isinstance(part, ToolCallPart):
                    tools.append(f"{part.tool_name}({_json(part.args)})")
                elif isinstance(part, ThinkingPart):
                    text = str(part.content or "").strip()
                    if text:
                        answers.append(text)

    sections = [
        "## 之前的对话摘要",
        f"- 用户请求：{_clip(' | '.join(users[-5:]), 1600)}",
        f"- 关键结果：{_clip(' | '.join(answers[-6:]), 2200)}",
        f"- 工具活动：{_clip(' | '.join(tools[-12:]), 1600)}",
        "- 注意：以下内容是压缩摘要，不应被当作原始逐字对话。",
    ]
    return _clip("\n".join(sections), limit)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def should_compact(used_tokens: int, total_tokens: int, threshold: float = 0.80) -> bool:
    if total_tokens <= 0:
        return False
    return used_tokens >= max(1, int(total_tokens * threshold))


def compact_history(messages: list[ModelMessage], summary: str | None = None) -> list[ModelMessage]:
    summary = summary or summarize_history(messages)
    return [
        ModelRequest(
            parts=[
                UserPromptPart(
                    content=(
                        "以下是之前对话的自动压缩摘要，请把它视为上下文背景，"
                        "不要重复展开已经完成的细节。\n\n" + summary
                    )
                )
            ]
        )
    ]


_HISTORY_ADAPTER: Any = None


def _init_adapter() -> None:
    global _HISTORY_ADAPTER
    if _HISTORY_ADAPTER is None:
        from pydantic import TypeAdapter

        _HISTORY_ADAPTER = TypeAdapter(list[ModelMessage])


_init_adapter()
