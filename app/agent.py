from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator

from pydantic_ai import (
    Agent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    RunContext,
)
from pydantic_ai.messages import (
    BinaryContent,
    FinalResultEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    UserContent,
)
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider

from .config import (
    LLAMA_BASE_URL,
    MAX_TOKENS,
    MODEL_NAME,
    TEMPERATURE,
    get_runtime_settings,
    system_prompt,
)
from .context import compact_history, estimate_tokens, should_compact, summarize_history
from .tools import (
    analyze_file,
    delete_path,
    diff_content,
    edit_file,
    grep_files,
    list_files,
    office_tool,
    organize_files,
    read_text_file,
    rename_files,
    run_shell,
    show_content,
    update_plan,
    web_search,
    write_text_file,
)


@dataclass
class AgentDeps:
    plan: list[dict[str, Any]]
    ask_key: str | None = None
    events: asyncio.Queue[dict[str, Any]] | None = None
    history: list[ModelMessage] | None = None


_ASK_FUTURES: dict[str, asyncio.Future[Any]] = {}


def resolve_ask(key: str, answer: str) -> bool:
    future = _ASK_FUTURES.get(key)
    if future is None or future.done():
        return False
    future.set_result(answer)
    return True


def cancel_ask(key: str | None) -> None:
    if not key:
        return
    future = _ASK_FUTURES.pop(key, None)
    if future is not None and not future.done():
        future.cancel()


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "dict"):
        return value.dict()
    return str(value)


def _args_json(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return _jsonable(raw)


def make_agent() -> Agent[AgentDeps, str]:
    model = OpenAIChatModel(
        MODEL_NAME,
        provider=OpenAIProvider(base_url=LLAMA_BASE_URL, api_key="local"),
    )
    agent: Agent[AgentDeps, str] = Agent(
        model,
        instructions=system_prompt(),
        deps_type=AgentDeps,
        retries=2,
        tool_timeout=45,
    )

    @agent.tool
    def shell(ctx: RunContext[AgentDeps], command: str, timeout: int = 30) -> str:
        """在工作区隔离 shell 中执行 Bash 命令，返回退出码、stdout 和 stderr。"""
        return run_shell(command, timeout)

    @agent.tool
    def read(
        ctx: RunContext[AgentDeps],
        path: str,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> str:
        """读取工作区 UTF-8 文本文件，支持可选行号范围。"""
        return read_text_file(path, start_line, end_line)

    @agent.tool
    def grep(
        ctx: RunContext[AgentDeps],
        pattern: str,
        path: str = ".",
        glob: str | None = None,
        case_sensitive: bool = False,
        max_results: int = 80,
    ) -> str:
        """在工作区内按 Python 正则表达式搜索文本。"""
        return grep_files(pattern, path, glob, case_sensitive, max_results)

    @agent.tool(name="diff")
    def diff_preview(
        ctx: RunContext[AgentDeps],
        old_path: str | None = None,
        new_path: str | None = None,
        old_text: str | None = None,
        new_text: str | None = None,
        context_lines: int = 3,
    ) -> str:
        """比较两个文件或文本并返回 unified diff，适合编辑前预览。"""
        return diff_content(old_path, new_path, old_text, new_text, context_lines)

    @agent.tool(name="ls")
    def list_directory(
        ctx: RunContext[AgentDeps],
        path: str = ".",
        recursive: bool = False,
        show_hidden: bool = False,
    ) -> Any:
        """列出目录内容，返回名称、类型、大小和修改时间。"""
        return list_files(path, recursive, show_hidden)

    @agent.tool(name="del")
    def delete_entry(
        ctx: RunContext[AgentDeps],
        path: str,
        recursive: bool = False,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> Any:
        """删除文件或目录；默认预览，确认后才执行。"""
        return delete_path(path, recursive, dry_run, confirm)

    @agent.tool(name="web_search")
    def search_web(
        ctx: RunContext[AgentDeps],
        query: str,
        max_results: int = 5,
        timeout: int = 10,
    ) -> Any:
        """使用 requests 搜索公开网页，返回标题、摘要和 URL。"""
        return web_search(query, max_results, timeout)

    @agent.tool(name="compact_context")
    def compact_context_tool(
        ctx: RunContext[AgentDeps],
        reason: str = "",
    ) -> Any:
        """压缩当前会话的旧上下文，返回摘要并提示主流程归档历史。"""
        history = ctx.deps.history or []
        summary = summarize_history(history)
        return {
            "ok": True,
            "reason": reason,
            "summary": summary,
            "message": "已生成上下文压缩摘要；主流程会将其保存为新的上下文起点。",
            "estimated_tokens_before": estimate_tokens(history),
        }

    @agent.tool(name="analyze")
    def analyze_attachment(
        ctx: RunContext[AgentDeps], path: str, detail: str = "summary"
    ) -> str:
        """分析 Word、PowerPoint、Excel、PDF 或文本附件，提取结构化内容。"""
        return analyze_file(path, detail)

    @agent.tool(name="show")
    def show_item(
        ctx: RunContext[AgentDeps],
        kind: str,
        target: str,
        title: str = "",
        caption: str = "",
    ) -> Any:
        """向用户展示内容。
        kind: image(图片) / url(网页,可点击进入) / table(JSON行数组) / mindmap(JSON树) / file / text / mermaid。
        table 的 target 示例：[{"名称":"A","数量":1}]；mindmap：{"label":"中心","children":[{"label":"分支"}]}。
        """
        return show_content(kind, target, title, caption)

    @agent.tool(name="office")
    def office(
        ctx: RunContext[AgentDeps],
        path: str,
        action: str = "inspect",
        sheet: str | None = None,
        range_ref: str | None = None,
        cells: dict[str, Any] | None = None,
        rows: list[dict[str, Any]] | None = None,
        text: str | None = None,
        create: str | None = None,
        confirm: bool = False,
    ) -> Any:
        """处理 Office 表格与文档（xlsx/csv/docx）。
        action: inspect | read | write | append_rows | create | create_doc | replace_text。
        写入类动作必须 confirm=true；read 返回可直接喂给 show kind=table 的结构。
        """
        return office_tool(
            path=path,
            action=action,
            sheet=sheet,
            range_ref=range_ref,
            cells=cells,
            rows=rows,
            text=text,
            create=create,
            confirm=confirm,
        )

    @agent.tool(name="edit")
    def edit_text(
        ctx: RunContext[AgentDeps],
        path: str,
        old_text: str = "",
        new_text: str = "",
        start_line: int | None = None,
        end_line: int | None = None,
        replace_all: bool = False,
    ) -> str:
        """精确编辑文本文件，支持 old_text 替换或按行号范围替换。"""
        return edit_file(path, old_text, new_text, start_line, end_line, replace_all)

    @agent.tool(name="rename")
    def rename_batch(
        ctx: RunContext[AgentDeps],
        directory: str = ".",
        pattern: str = "",
        replacement: str = "",
        glob: str | None = None,
        recursive: bool = False,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> str:
        """按正则表达式批量重命名文件；默认预览，确认后再执行。"""
        return rename_files(directory, pattern, replacement, glob, recursive, dry_run, confirm)

    @agent.tool(name="organize")
    def organize_directory(
        ctx: RunContext[AgentDeps],
        directory: str = ".",
        mode: str = "extension",
        recursive: bool = False,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> str:
        """按文件类型或修改日期整理目录；默认预览，确认后再移动。"""
        return organize_files(directory, mode, recursive, dry_run, confirm)

    @agent.tool
    def write(
        ctx: RunContext[AgentDeps], path: str, content: str, append: bool = False
    ) -> str:
        """创建或覆盖工作区文本文件；append=True 时追加内容。"""
        return write_text_file(path, content, append)

    @agent.tool(name="askuser", timeout=3600)
    async def ask_user(
        ctx: RunContext[AgentDeps],
        question: str,
        options: list[str] | None = None,
        multi_select: bool = False,
    ) -> Any:
        """向用户提出问题并等待网页端回答，适合处理歧义或需要确认的选择。"""
        key = ctx.deps.ask_key
        queue = ctx.deps.events
        if not key or queue is None:
            return {"ok": False, "error": "当前运行没有可用的 askuser 会话"}
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        _ASK_FUTURES[key] = future
        queue.put_nowait(
            {
                "type": "ask_user",
                "key": key,
                "question": question,
                "options": options or [],
                "multi_select": bool(multi_select),
            }
        )
        try:
            answer = await future
        finally:
            _ASK_FUTURES.pop(key, None)
        return {"ok": True, "question": question, "answer": answer}

    @agent.tool
    def plan(
        ctx: RunContext[AgentDeps],
        title: str,
        steps: list[str],
        completed_steps: list[int] | None = None,
    ) -> str:
        """设置或更新多步骤执行计划，完成步骤时用 1 起始编号标记。"""
        return update_plan(title, steps, ctx.deps.plan, completed_steps)

    return agent


AGENT = make_agent()


def make_title_agent() -> Agent[None, str]:
    model = OpenAIChatModel(
        MODEL_NAME,
        provider=OpenAIProvider(base_url=LLAMA_BASE_URL, api_key="local"),
    )
    return Agent(
        model,
        instructions=(
            "你是一个会话标题生成器。只输出一个简短、具体、适合侧栏显示的中文标题，"
            "不超过24个字。不要输出引号、Markdown、解释、换行或任务总结。"
        ),
        output_type=str,
        retries=1,
    )


TITLE_AGENT = make_title_agent()


def _clean_title(value: str) -> str:
    title = str(value or "").strip()
    title = title.strip("`*_#\"'“” ")
    title = title.splitlines()[0] if title.splitlines() else title
    title = title[:48].strip()
    return title or "新对话"


async def generate_session_title(text: str) -> str:
    try:
        source = str(text or "").strip()[:1200]
        result = await TITLE_AGENT.run(
            "请为以下内容生成一个不超过24字的中文标题，只输出标题：\n" + source,
            model_settings={
                "temperature": 0.1,
                "max_tokens": 512,
                "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
            },
        )
        title = _clean_title(result.output)
        if len(title) > 30 or title == source or source.startswith(title) and len(title) > 24:
            fallback = re.split(r"[，。；！？,;:!?\n]", source, maxsplit=1)[0]
            title = _clean_title(fallback)
        return title[:24]
    except Exception:
        from .sessions import generate_title

        return generate_title(text)


async def stream_run(
    *,
    message: str | list[UserContent],
    history: list[ModelMessage],
    plan: list[dict[str, Any]],
    events: asyncio.Queue[dict[str, Any]],
    ask_key: str | None = None,
) -> tuple[list[ModelMessage], list[ModelMessage]]:
    """Run one agent turn, forwarding typed framework events to an SSE queue.

    ``message`` may be plain text or a pydantic-ai UserContent list
    (text + BinaryContent images for multimodal models).
    """
    turn_tools: list[dict[str, Any]] = []
    pending_tools: set[str] = set()
    tool_names: dict[str, str] = {}
    tool_arg_buffers: dict[str, str] = {}
    loop_step = 0
    loop_ended = False
    loop_end_steps: set[int] = set()

    async def handler(ctx: RunContext[AgentDeps], stream: AsyncIterator[Any]) -> None:
        nonlocal loop_step, loop_ended
        async for event in stream:
            if isinstance(event, PartStartEvent) and event.index == 0:
                loop_step += 1
                events.put_nowait({"type": "loop_start", "step": loop_step})
            if isinstance(event, FinalResultEvent) and not loop_ended:
                loop_ended = True
                loop_end_steps.add(loop_step)
                events.put_nowait(
                    {"type": "loop_end", "step": loop_step, "final": True}
                )
            if isinstance(event, PartStartEvent):
                part = getattr(event, "part", None)
                if isinstance(part, ToolCallPart):
                    tool_names[part.tool_call_id] = part.tool_name
                    if part.tool_name == "plan":
                        args = part.args
                        if isinstance(args, dict):
                            args = json.dumps(args, ensure_ascii=False)
                        tool_arg_buffers[part.tool_call_id] = args or ""
                        events.put_nowait(
                            {
                                "type": "plan_preview",
                                "step": loop_step,
                                "args": _args_json(tool_arg_buffers[part.tool_call_id]),
                            }
                        )
                if isinstance(part, TextPart) and part.content:
                    events.put_nowait(
                        {"type": "delta", "delta": part.content, "step": loop_step}
                    )
                elif isinstance(part, ThinkingPart) and part.content:
                    events.put_nowait(
                        {
                            "type": "reasoning_delta",
                            "delta": part.content,
                            "step": loop_step,
                        }
                    )
            elif isinstance(event, PartDeltaEvent):
                delta = getattr(event, "delta", None)
                tool_call_id = getattr(delta, "tool_call_id", None)
                args_delta = getattr(delta, "args_delta", None)
                if tool_call_id and args_delta is not None and tool_names.get(tool_call_id) == "plan":
                    if isinstance(args_delta, str):
                        tool_arg_buffers[tool_call_id] = tool_arg_buffers.get(tool_call_id, "") + args_delta
                    else:
                        tool_arg_buffers[tool_call_id] = json.dumps(args_delta, ensure_ascii=False)
                    events.put_nowait(
                        {
                            "type": "plan_preview",
                            "step": loop_step,
                            "args": _args_json(tool_arg_buffers[tool_call_id]),
                        }
                    )
                if isinstance(delta, TextPartDelta) and delta.content_delta:
                    events.put_nowait(
                        {"type": "delta", "delta": delta.content_delta, "step": loop_step}
                    )
                elif isinstance(delta, ThinkingPartDelta) and delta.content_delta:
                    events.put_nowait(
                        {
                            "type": "reasoning_delta",
                            "delta": delta.content_delta,
                            "step": loop_step,
                        }
                    )
            elif isinstance(event, FunctionToolCallEvent):
                tool_item = {
                    "id": event.tool_call_id,
                    "name": event.part.tool_name,
                    "args": _args_json(event.part.args),
                    "result": None,
                    "step": loop_step,
                }
                turn_tools.append(tool_item)
                pending_tools.add(event.tool_call_id)
                tool_names[event.tool_call_id] = event.part.tool_name
                if event.part.tool_name == "plan":
                    events.put_nowait(
                        {
                            "type": "plan_preview",
                            "step": loop_step,
                            "args": _args_json(event.part.args),
                        }
                    )
                events.put_nowait({"type": "tool_call", **tool_item})
            elif isinstance(event, FunctionToolResultEvent):
                content = getattr(event.part, "content", None)
                outcome = getattr(event.part, "outcome", "success")
                item = next(
                    (
                        tool
                        for tool in turn_tools
                        if tool["id"] == event.tool_call_id
                    ),
                    None,
                )
                payload = {
                    "type": "tool_result",
                    "id": event.tool_call_id,
                    "name": getattr(event.part, "tool_name", ""),
                    "content": _jsonable(content),
                    "outcome": outcome,
                    "step": loop_step,
                }
                if item is not None:
                    item["result"] = payload["content"]
                    item["outcome"] = outcome
                pending_tools.discard(event.tool_call_id)
                events.put_nowait(payload)
                if not pending_tools and loop_step not in loop_end_steps:
                    loop_end_steps.add(loop_step)
                    events.put_nowait(
                        {"type": "loop_end", "step": loop_step, "final": False}
                    )

    result = await AGENT.run(
        message,  # str | Sequence[UserContent]
        message_history=history,
        instructions=system_prompt(),
        deps=AgentDeps(plan=plan, ask_key=ask_key, events=events, history=history),
        event_stream_handler=handler,
        model_settings={
            "temperature": TEMPERATURE,
            "max_tokens": int(get_runtime_settings().get("max_tokens", MAX_TOKENS)),
        },
    )
    if not loop_ended and loop_step not in loop_end_steps:
        events.put_nowait(
            {"type": "loop_end", "step": loop_step, "final": True}
        )
    all_messages = result.all_messages()
    current_messages = all_messages[len(history) :]
    return all_messages, current_messages


def display_assistant_from_result(
    result_messages: list[ModelMessage], text: str, tools: list[dict[str, Any]]
) -> dict[str, Any]:
    reasoning_parts: list[str] = []
    blocks: list[dict[str, Any]] = []
    tool_blocks: dict[str, dict[str, Any]] = {}
    response_index = -1

    for message in result_messages:
        if isinstance(message, ModelResponse):
            response_index += 1
            thinking = [
                part.content
                for part in message.parts
                if isinstance(part, ThinkingPart)
            ]
            if thinking:
                joined = "\n".join(thinking)
                if response_index == 0:
                    reasoning_parts.append(joined)
                    blocks.append({"type": "reasoning", "content": joined})
                else:
                    blocks.append({"type": "later_reasoning", "content": joined})
            for part in message.parts:
                if isinstance(part, TextPart) and part.content:
                    blocks.append({"type": "text", "content": part.content})
                elif isinstance(part, ToolCallPart):
                    args = part.args
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            pass
                    block = {
                        "type": "tool",
                        "id": part.tool_call_id,
                        "name": part.tool_name,
                        "args": args,
                        "result": None,
                    }
                    blocks.append(block)
                    tool_blocks[part.tool_call_id] = block
        elif isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, ToolReturnPart) and part.tool_call_id in tool_blocks:
                    tool_blocks[part.tool_call_id]["result"] = _jsonable(part.content)
                    tool_blocks[part.tool_call_id]["outcome"] = part.outcome

    if text and not any(
        block.get("type") == "text" and block.get("content") == text
        for block in blocks
    ):
        blocks.append({"type": "text", "content": text})

    return {
        "role": "assistant",
        "content": text,
        "reasoning": "\n".join(reasoning_parts),
        "tools": tools,
        "blocks": blocks,
    }
