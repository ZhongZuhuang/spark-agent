from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from pydantic_ai.messages import (
    BinaryContent,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserContent,
    UserPromptPart,
)

from .agent import (
    AGENT,
    cancel_ask,
    display_assistant_from_result,
    generate_session_title,
    resolve_ask,
    stream_run,
)
from .context import compact_history, estimate_tokens, should_compact, summarize_history
from .config import (
    HISTORY_LIMIT,
    LLAMA_BASE_URL,
    LLAMA_PORT,
    MODEL_DIR,
    MODEL_NAME,
    MODEL_PATH,
    ROOT_DIR,
    SESSION_LIMIT,
    WEB_HOST,
    WEB_PORT,
    describe_model,
    find_llama_server,
    get_runtime_settings,
    get_workspace,
    list_gguf_models,
    model_capabilities,
    set_runtime_settings,
    set_workspace,
)
from .sessions import (
    context_view,
    generate_title,
    get_or_create,
    load_sessions,
    public_view,
    save_sessions,
)

app = FastAPI(title="Spark Agent", version="1.0.0")


@app.middleware("http")
async def disable_static_cache(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    # HTML must never be sticky-cached; assets revalidate every time.
    if path in {"/", "/about", "/settings"}:
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    elif path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache, max-age=0, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response

SESSIONS = load_sessions()
SESSION_LOCKS: dict[str, asyncio.Lock] = {}

STATIC_DIR = ROOT_DIR / "static"
_ACTIVE_CHATS = 0
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS = 10
RUN_RETENTION_SECONDS = 180


def _lock_for(session_id: str) -> asyncio.Lock:
    return SESSION_LOCKS.setdefault(session_id, asyncio.Lock())


@dataclass
class ActiveRun:
    """One in-flight agent run, decoupled from any single SSE connection."""

    session_id: str
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribers: set[asyncio.Queue[dict[str, Any]]] = field(default_factory=set)
    worker: asyncio.Task[Any] | None = None
    finished: bool = False
    finished_at: float | None = None

    def publish(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        for subscriber in list(self.subscribers):
            subscriber.put_nowait(event)
        if event.get("type") == "finish":
            self.finished = True
            self.finished_at = time.time()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        for event in self.events:
            queue.put_nowait(event)
        if not self.finished:
            self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self.subscribers.discard(queue)

    def is_running(self) -> bool:
        if self.finished:
            return False
        if self.worker is None:
            return True
        return not self.worker.done()


ACTIVE_RUNS: dict[str, ActiveRun] = {}


def _cleanup_stale_runs() -> None:
    now = time.time()
    for sid in list(ACTIVE_RUNS):
        run = ACTIVE_RUNS[sid]
        if (
            run.finished
            and run.finished_at is not None
            and now - run.finished_at >= RUN_RETENTION_SECONDS
            and not run.subscribers
        ):
            ACTIVE_RUNS.pop(sid, None)


def _get_run(sid: str) -> ActiveRun | None:
    _cleanup_stale_runs()
    return ACTIVE_RUNS.get(sid)


def is_session_running(session_id: str) -> bool:
    run = ACTIVE_RUNS.get(session_id)
    return bool(run and run.is_running())


def _tools_from_history(messages: list[Any]) -> list[dict[str, Any]]:
    calls: dict[str, dict[str, Any]] = {}
    for message in messages:
        if isinstance(message, ModelResponse):
            for part in message.parts:
                if isinstance(part, ToolCallPart):
                    args = part.args
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except json.JSONDecodeError:
                            pass
                    calls[part.tool_call_id] = {
                        "id": part.tool_call_id,
                        "name": part.tool_name,
                        "args": args,
                        "result": None,
                        "outcome": None,
                    }
        elif isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, ToolReturnPart) and part.tool_call_id in calls:
                    calls[part.tool_call_id]["result"] = _jsonable(part.content)
                    calls[part.tool_call_id]["outcome"] = part.outcome
    return list(calls.values())


def _trim_history(messages: list[Any]) -> list[Any]:
    if len(messages) <= HISTORY_LIMIT:
        return messages
    trimmed = messages[-HISTORY_LIMIT:]
    while trimmed and not (
        isinstance(trimmed[0], ModelRequest)
        and any(isinstance(part, UserPromptPart) for part in trimmed[0].parts)
    ):
        trimmed.pop(0)
    return trimmed


def _jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)


def _clean_session_id(value: str | None) -> str:
    candidate = (value or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", candidate):
        return "default-" + uuid.uuid4().hex[:16]
    return candidate


def _sse(payload: dict[str, Any]) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _save_attachment(attachment: AttachmentUpload) -> Path:
    raw_name = Path(attachment.filename.replace("\\", "/")).name.strip()
    if not raw_name or raw_name in {".", ".."} or "\x00" in raw_name:
        raise ValueError(f"附件文件名非法：{attachment.filename}")
    try:
        data = base64.b64decode(attachment.data_base64, validate=True)
    except Exception as exc:
        raise ValueError(f"附件 base64 解码失败：{raw_name}") from exc
    if not data:
        raise ValueError(f"附件为空：{raw_name}")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"附件超过 {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MiB：{raw_name}")

    directory = get_workspace() / ".attachments"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / raw_name
    if target.exists():
        stem, suffix = target.stem, target.suffix
        counter = 1
        while target.exists():
            target = directory / f"{stem} ({counter}){suffix}"
            counter += 1
    target.write_bytes(data)
    return target


_IMAGE_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".avif": "image/avif",
}
_IMAGE_MIMES = set(_IMAGE_MIME_BY_EXT.values()) | {"image/jpg", "image/x-png"}


def _attachment_mime(attachment: AttachmentUpload) -> str:
    mime = (attachment.mime_type or "").split(";")[0].strip().lower()
    if mime in _IMAGE_MIMES:
        return "image/jpeg" if mime == "image/jpg" else mime
    ext = Path(attachment.filename).suffix.lower()
    if ext in _IMAGE_MIME_BY_EXT:
        return _IMAGE_MIME_BY_EXT[ext]
    guessed = mimetypes.guess_type(attachment.filename)[0]
    return guessed or mime or "application/octet-stream"


def _is_image_attachment(attachment: AttachmentUpload) -> bool:
    mime = _attachment_mime(attachment)
    if mime.startswith("image/"):
        # SVG is text-ish for llama multimodal — treat as image only if png/jpeg family
        return mime in _IMAGE_MIMES or mime in {"image/png", "image/jpeg", "image/webp", "image/gif"}
    ext = Path(attachment.filename).suffix.lower()
    return ext in _IMAGE_MIME_BY_EXT


def _build_user_prompt(
    message: str,
    attachments: list[AttachmentUpload],
    saved_paths: list[str],
    *,
    allow_vision: bool,
) -> str | list[UserContent]:
    """Plain text when no images; otherwise text + BinaryContent for the VL model."""
    image_atts = [a for a in attachments if _is_image_attachment(a)]
    other_atts = [a for a in attachments if not _is_image_attachment(a)]

    lines = [message.strip() or "请分析这些附件。"]
    if saved_paths:
        lines.append("")
        if image_atts and allow_vision:
            lines.append("已附带图片（模型可直接看图）；其他附件路径如下，可用 office/analyze 读取：")
        else:
            lines.append("已上传附件（请使用 office / analyze 工具读取）：")
        for path in saved_paths:
            lines.append(f"- {path}")
        if image_atts and not allow_vision:
            lines.append("")
            lines.append("注意：当前模型无视觉能力，图片仅保存为文件，无法直接看图。")

    text = "\n".join(lines)
    if not (allow_vision and image_atts):
        return text

    parts: list[UserContent] = [text]
    for att in image_atts:
        try:
            raw = base64.b64decode(att.data_base64, validate=True)
        except Exception:
            continue
        if not raw:
            continue
        mime = _attachment_mime(att)
        try:
            parts.append(BinaryContent(data=raw, media_type=mime))  # type: ignore[arg-type]
        except Exception:
            # Fallback: skip broken image rather than fail the whole turn
            continue
    if len(parts) == 1:
        return text
    return parts


def _fetch_json(url: str, timeout: float = 2.0) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


# Brief cache so health / about / settings don't stack probes on every paint.
_HEALTH_CACHE: dict[str, Any] = {"at": 0.0, "data": None}
_HEALTH_CACHE_TTL = 0.5
# Manual stop: watchdog / auto-start must NOT resurrect llama until user starts it.
LLAMA_MANUAL_STOP_FLAG = ROOT_DIR / ".run" / "llama.manual_stop"


def _llama_manual_stopped() -> bool:
    try:
        return LLAMA_MANUAL_STOP_FLAG.is_file()
    except OSError:
        return False


def _set_llama_manual_stop(stopped: bool) -> None:
    try:
        LLAMA_MANUAL_STOP_FLAG.parent.mkdir(parents=True, exist_ok=True)
        if stopped:
            LLAMA_MANUAL_STOP_FLAG.write_text(
                f"stopped_at={time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n",
                encoding="utf-8",
            )
        else:
            LLAMA_MANUAL_STOP_FLAG.unlink(missing_ok=True)
    except OSError:
        pass


def _systemd_user_unit(action: str, unit: str, timeout: float = 30.0) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            ["systemctl", "--user", action, unit],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return {
            "ok": proc.returncode == 0,
            "action": action,
            "unit": unit,
            "stdout": (proc.stdout or "").strip()[-400:],
            "stderr": (proc.stderr or "").strip()[-400:],
            "code": proc.returncode,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "action": action, "unit": unit, "error": str(exc)}


def get_model_health() -> dict[str, Any]:
    now = time.monotonic()
    cached = _HEALTH_CACHE.get("data")
    if cached and (now - float(_HEALTH_CACHE.get("at") or 0)) < _HEALTH_CACHE_TTL:
        return cached

    settings = get_runtime_settings()
    base = LLAMA_BASE_URL.removesuffix("/v1")
    model_file = describe_model(settings.get("model"))
    health: dict[str, Any] = {
        "model_healthy": False,
        "llama_healthy": False,
        "llama_manual_stopped": _llama_manual_stopped(),
        "context_used": 0,
        "context_total": int(settings["context_size"]),
        "context_percent": 0,
        "kv_cache": settings["kv_cache"],
        "backend_profile": settings["backend_profile"],
        "settings": settings,
        "model_file": model_file,
        "model_label": model_file.get("name") or MODEL_NAME,
        "model_alias": MODEL_NAME,
        "errors": [],
    }

    # Parallel probes — 3× sequential 2s timeouts were the slow path.
    probe_timeout = 0.6
    with ThreadPoolExecutor(max_workers=3) as pool:
        f_health = pool.submit(_fetch_json, base + "/health", probe_timeout)
        f_models = pool.submit(_fetch_json, LLAMA_BASE_URL + "/models", probe_timeout)
        f_slots = pool.submit(_fetch_json, base + "/slots", probe_timeout)

        try:
            payload = f_health.result()
            health["llama_healthy"] = payload.get("status") == "ok"
            health["health_payload"] = payload
        except Exception as exc:
            health["errors"].append(f"health: {exc}")

        try:
            payload = f_models.result()
            data = payload.get("data", []) if isinstance(payload, dict) else []
            health["model_healthy"] = any(
                str(item.get("id", "")).lower() == MODEL_NAME.lower() for item in data
            ) if data else bool(health["llama_healthy"])
            health["models"] = data
        except Exception as exc:
            health["errors"].append(f"models: {exc}")

        try:
            slots = f_slots.result()
            if isinstance(slots, list) and slots:
                slot = slots[0]
                health["context_total"] = int(slot.get("n_ctx") or health["context_total"])
                health["context_used"] = int(slot.get("n_prompt_tokens") or 0)
                health["slot"] = slot
        except Exception as exc:
            health["errors"].append(f"slots: {exc}")

    total = max(1, int(health["context_total"]))
    health["context_percent"] = round(100 * int(health["context_used"]) / total, 1)
    health["healthy"] = bool(health["llama_healthy"] and health["model_healthy"])
    _HEALTH_CACHE["at"] = time.monotonic()
    _HEALTH_CACHE["data"] = health
    return health


def _llama_health() -> dict[str, Any]:
    base = LLAMA_BASE_URL.removesuffix("/v1")
    url = base + "/health"
    try:
        with urllib.request.urlopen(url, timeout=0.6) as response:
            body = response.read().decode("utf-8", errors="replace")
        status = "ok" if response.status == 200 else str(response.status)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"status": "offline", "error": str(exc), "base_url": LLAMA_BASE_URL}
    return {"status": status, "base_url": LLAMA_BASE_URL, "raw": body[:500]}


class AttachmentUpload(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    data_base64: str = Field(min_length=1, max_length=40_000_000)
    mime_type: str = Field(default="application/octet-stream", max_length=255)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=100_000)
    session_id: str | None = None
    attachments: list[AttachmentUpload] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)


class WorkspaceRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class SettingsRequest(BaseModel):
    values: dict[str, Any] = Field(default_factory=dict)


def _pids_on_port(port: int) -> list[int]:
    """PIDs currently listening on TCP port (Linux ss)."""
    try:
        out = subprocess.check_output(
            ["ss", "-ltnp"], text=True, stderr=subprocess.DEVNULL, timeout=2
        )
    except Exception:
        return []
    pids: list[int] = []
    needle = f":{port} "
    for line in out.splitlines():
        if needle not in line and f":{port}\t" not in line:
            continue
        for match in re.finditer(r"pid=(\d+)", line):
            pid = int(match.group(1))
            if pid and pid not in pids:
                pids.append(pid)
    return pids


def _terminate_pid(pid: int, timeout: float = 3.0) -> None:
    if pid <= 0:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _restart_llama(settings: dict[str, Any]) -> dict[str, Any]:
    pid_file = ROOT_DIR / ".run" / "llama.pid"
    log_path = ROOT_DIR / ".run" / "llama.log"

    # 1) Stop pid-file process AND any leftover listener on the port.
    #    Otherwise /health answers the OLD process and we falsely "succeed".
    stale_pids: list[int] = []
    if pid_file.exists():
        try:
            stale_pids.append(int(pid_file.read_text().strip()))
        except (OSError, ValueError):
            pass
    for pid in _pids_on_port(LLAMA_PORT):
        if pid not in stale_pids:
            stale_pids.append(pid)
    for pid in stale_pids:
        _terminate_pid(pid)

    # 2) Wait until the port is actually free.
    for _ in range(50):
        if not _pids_on_port(LLAMA_PORT):
            break
        time.sleep(0.1)
    else:
        raise HTTPException(
            status_code=500,
            detail=f"端口 {LLAMA_PORT} 仍被占用，无法重启 llama.cpp",
        )

    from .config import find_llama_server

    binary = find_llama_server(str(settings.get("llama_bin") or ""))
    if not binary:
        raise HTTPException(
            status_code=500,
            detail="找不到 llama-server。请在 设置 → 运行环境 填写完整路径，或设置 SPARK_LLAMA_BIN。",
        )
    model_file = describe_model(settings.get("model"))
    if model_file.get("error") or not model_file.get("path"):
        raise HTTPException(
            status_code=400,
            detail=model_file.get("error") or "模型路径无效",
        )
    model_path = model_file["path"]
    command = [
        binary,
        "--model", model_path,
        "--host", "127.0.0.1",
        "--port", str(LLAMA_PORT),
        "--ctx-size", str(settings["context_size"]),
        "--parallel", "1",
        "--n-gpu-layers", str(settings["gpu_layers"]),
        "--flash-attn", str(settings["flash_attention"]),
        "--cache-type-k", str(settings["kv_cache"]),
        "--cache-type-v", str(settings["kv_cache"]),
        "--threads", str(settings["threads"]),
        "--threads-batch", str(settings["threads_batch"]),
        "--n-predict", str(int(settings.get("max_tokens") or -1)),
        "--cont-batching",
        "--alias", MODEL_NAME,
    ]
    # Vision model: attach its projector so image input works (unless disabled).
    if model_file.get("mmproj") and settings.get("enable_vision", True):
        command[3:3] = ["--mmproj", str(model_file["mmproj"])]
    elif model_file.get("mmproj") and not settings.get("enable_vision", True):
        # Explicitly disable auto mmproj download/use when user turned vision off.
        command.append("--no-mmproj")

    # Capability flags derived from the GGUF + user toggles.
    caps = model_capabilities(model_path)
    notes: list[str] = []
    if caps.get("mtp"):
        # Current llama.cpp often loads nextn.* as unused tensors. Preference is
        # still persisted so a build with MTP support can honor it later; we
        # surface the state instead of silently ignoring the toggle.
        if settings.get("enable_mtp", True):
            notes.append(
                "检测到 MTP（nextn）层：已启用偏好。"
                "若当前 llama.cpp 未实现 MTP，日志会出现 unused tensor，属预期。"
            )
        else:
            notes.append("MTP 层已按设置关闭偏好（当前构建若本就不使用 nextn，则无行为差异）。")
    if caps.get("diffusion"):
        notes.append("检测到扩散/多模态生成相关张量：该模型可能不适合纯文本对话。")
    if caps.get("embedding"):
        notes.append("检测到 Embedding 特征（pooling/bert），不适合聊天。")
    if settings.get("enable_speculative") and not caps.get("mtp"):
        notes.append("已打开 speculative 偏好，但当前模型没有可用 draft/MTP 层。")

    # 3) Launch the NEW process and record the exact argv.
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n[spark] {time.strftime('%Y-%m-%d %H:%M:%S')} restart argv: {command}\n")
        log.flush()
        process = subprocess.Popen(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    pid_file.write_text(str(process.pid), encoding="utf-8")

    # 4) Wait until *this* pid is healthy (not some leftover on the port).
    for _ in range(240):
        if process.poll() is not None:
            tail = ""
            try:
                lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                tail = "\n".join(lines[-14:])
            except OSError:
                pass
            detail = f"llama.cpp 启动失败（pid {process.pid} 已退出）"
            low = tail.lower()
            if "cudaMalloc" in tail or "out of memory" in low or "failed to allocate" in low:
                detail = (
                    "llama.cpp 启动失败：GPU 显存不足（CUDA OOM）。\n"
                    f"当前设置 gpu_layers={settings.get('gpu_layers')}、"
                    f"ctx={settings.get('context_size')}、模型 {model_file.get('name')}。\n"
                    "请调低 GPU Layers（例如 0 = 纯 CPU）或换更小的模型/上下文后再保存。\n"
                    f"argv: {' '.join(command)}\n"
                    f"日志:\n{tail}"
                )
            elif tail:
                detail += f"：\n{tail}"
            else:
                detail += "，请查看 .run/llama.log"
            raise HTTPException(status_code=500, detail=detail)
        try:
            payload = _fetch_json(f"http://127.0.0.1:{LLAMA_PORT}/health", timeout=1)
            if payload.get("status") == "ok":
                # Health must come from a live child, not a stale listener.
                if process.poll() is None:
                    break
        except Exception:
            pass
        time.sleep(0.5)
    else:
        raise HTTPException(status_code=504, detail="等待 llama.cpp 启动超时")

    return {
        "ok": True,
        "pid": process.pid,
        "settings": settings,
        "model": model_file,
        "capabilities": caps,
        "capability_notes": notes,
        "argv": command,
    }


class TitleRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)
    title: str = Field(min_length=1, max_length=120)


class ResetRequest(BaseModel):
    session_id: str | None = None


class OpenRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class AnswerRequest(BaseModel):
    key: str = Field(min_length=8, max_length=256)
    answer: str = Field(min_length=0, max_length=20_000)


@app.get("/api/health")
def health() -> dict[str, Any]:
    settings = get_runtime_settings()
    model_file = describe_model(settings.get("model"))
    return {
        "web": "ok",
        "llama": _llama_health(),
        "model": model_file.get("name") or MODEL_NAME,
        "model_file": model_file,
        "capabilities": model_capabilities(settings.get("model")),
        "model_alias": MODEL_NAME,
        "model_dir": str(MODEL_DIR),
        "workspace": str(get_workspace()),
        "tools": [
            "shell",
            "read",
            "grep",
            "ls",
            "del",
            "write",
            "edit",
            "diff",
            "plan",
            "askuser",
            "web_search",
            "compact_context",
            "analyze",
            "show",
            "office",
            "rename",
            "organize",
        ],
        "attachments": True,
        "max_attachment_mib": MAX_ATTACHMENT_BYTES // (1024 * 1024),
        "active_chats": _ACTIVE_CHATS,
        "model_health": get_model_health(),
        "llama_manual_stopped": _llama_manual_stopped(),
        "llama_bin": str(settings.get("llama_bin") or ""),
        "llama_bin_resolved": find_llama_server(str(settings.get("llama_bin") or "")),
        "model_dir": str(settings.get("model_dir") or MODEL_DIR),
        "framework": "Pydantic AI",
    }


@app.get("/api/models")
def list_models() -> dict[str, Any]:
    settings = get_runtime_settings()
    current = str(settings.get("model") or "")
    items = list_gguf_models()
    for item in items:
        item["selected"] = item["path"] == current
    return {
        "model_dir": str(MODEL_DIR),
        "current": current,
        "current_info": describe_model(current),
        "models": items,
    }


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    settings = get_runtime_settings()
    model_info = describe_model(settings.get("model"))
    caps = model_capabilities(settings.get("model"))
    return {
        "settings": settings,
        "restart_required": False,
        "model_dir": str(MODEL_DIR),
        "models": list_gguf_models(),
        "model_info": model_info,
        "capabilities": caps,
        "model_dir": str(settings.get("model_dir") or MODEL_DIR),
        "llama_bin": str(settings.get("llama_bin") or ""),
        "llama_bin_resolved": find_llama_server(str(settings.get("llama_bin") or "")),
    }


@app.post("/api/settings/apply")
def apply_settings(payload: SettingsRequest) -> dict[str, Any]:
    if _ACTIVE_CHATS:
        raise HTTPException(status_code=409, detail="Agent 正在执行任务，请稍后再修改设置")
    try:
        settings = set_runtime_settings(payload.values)
        # Settings apply implies the user wants the model running.
        _set_llama_manual_stop(False)
        _systemd_user_unit("stop", "spark-llama.service", timeout=20)
        restarted = _restart_llama(settings)
        # Keep systemd unit in sync with the process we just spawned.
        _systemd_user_unit("reset-failed", "spark-llama.service", timeout=10)
        # Drop cached health so context/model reflect the new process immediately.
        _HEALTH_CACHE["at"] = 0.0
        _HEALTH_CACHE["data"] = None
    except HTTPException:
        raise
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "settings": settings,
        "restart_required": False,
        "llama": restarted,
        "model": restarted.get("model"),
        "argv": restarted.get("argv"),
        "message": "设置已保存并重启 llama.cpp",
    }


@app.post("/api/settings")
def update_settings(payload: SettingsRequest) -> dict[str, Any]:
    if _ACTIVE_CHATS:
        raise HTTPException(status_code=409, detail="Agent 正在执行任务，请稍后再修改设置")
    try:
        settings = set_runtime_settings(payload.values)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "settings": settings,
        "restart_required": True,
        "message": "设置已保存；重启 Spark 后生效",
    }


@app.get("/api/sessions")
def list_sessions() -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for sid, session in SESSIONS.items():
        messages = session.get("messages", [])
        if not messages:
            continue
        first_user = next(
            (
                str(message.get("content", "")).strip().splitlines()[0]
                for message in messages
                if message.get("role") == "user" and str(message.get("content", "")).strip()
            ),
            "新对话",
        )
        items.append(
            {
                "session_id": sid,
                "title": str(session.get("title") or first_user)[:120] or "新对话",
                "message_count": len(messages),
                "plan_count": len(session.get("plan", [])),
                "updated_at": session.get("updated_at"),
            }
        )
    items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return {"sessions": items, "active_chats": _ACTIVE_CHATS}


@app.get("/api/session")
def get_session(session_id: str | None = None) -> dict[str, Any]:
    sid = _clean_session_id(session_id)
    session = get_or_create(SESSIONS, sid)
    view = public_view(session)
    view["running"] = is_session_running(sid)
    return {"session_id": sid, **view, "workspace": str(get_workspace())}


@app.get("/api/context")
def get_context(session_id: str | None = None) -> dict[str, Any]:
    sid = _clean_session_id(session_id)
    session = get_or_create(SESSIONS, sid)
    view = context_view(session)
    model_health = get_model_health()
    view.update(
        {
            "estimated_tokens": estimate_tokens(session.get("history", [])),
            "context_total": model_health["context_total"],
            "context_used": model_health["context_used"],
            "context_percent": model_health["context_percent"],
            "model_healthy": model_health["model_healthy"],
            "settings": model_health["settings"],
        }
    )
    return {"session_id": sid, **view}


@app.get("/api/context/live")
def context_live() -> dict[str, Any]:
    """Real context occupancy from llama.cpp /slots (prompt + decoded so far)."""
    settings = get_runtime_settings()
    base = LLAMA_BASE_URL.removesuffix("/v1")
    total = int(settings["context_size"])
    used = 0
    processed = False
    decoding = 0
    errors: list[str] = []
    try:
        slots = _fetch_json(base + "/slots", timeout=1.5)
        if isinstance(slots, list) and slots:
            slot = slots[0]
            total = int(slot.get("n_ctx") or total)
            used = int(slot.get("n_prompt_tokens") or 0)
            processed = bool(slot.get("n_prompt_tokens_processed"))
            # llama.cpp returns next_token as a list of token-info objects.
            raw_next = slot.get("next_token")
            if isinstance(raw_next, list) and raw_next:
                nxt = raw_next[0] if isinstance(raw_next[0], dict) else {}
            elif isinstance(raw_next, dict):
                nxt = raw_next
            else:
                nxt = {}
            decoding = int(nxt.get("n_decoded") or 0)
    except Exception as exc:
        errors.append(str(exc))
    # While generating, decoded tokens also occupy the context window.
    total_used = used + max(0, decoding)
    percent = round(100 * total_used / max(1, total), 1)
    return {
        "ok": not errors,
        "used": total_used,
        "prompt_tokens": used,
        "decoded_tokens": max(0, decoding),
        "processed": processed,
        "total": total,
        "percent": percent,
        "errors": errors,
        "source": "llama.slots" if not errors else "error",
    }


@app.post("/api/workspace")
def switch_workspace(payload: WorkspaceRequest) -> dict[str, Any]:
    if _ACTIVE_CHATS:
        raise HTTPException(status_code=409, detail="Agent 正在执行任务，请稍后再切换工作目录")
    try:
        path = set_workspace(payload.path.strip())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "workspace": str(path)}


@app.patch("/api/session/title")
def update_session_title(payload: TitleRequest) -> dict[str, Any]:
    if _ACTIVE_CHATS:
        raise HTTPException(status_code=409, detail="Agent 正在执行任务，请稍后再修改标题")
    session = get_or_create(SESSIONS, payload.session_id)
    session["title"] = payload.title.strip()[:120] or "新对话"
    session["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    save_sessions(SESSIONS)
    return {"ok": True, "session_id": payload.session_id, "title": session["title"]}


@app.delete("/api/session")
def delete_session(session_id: str | None = None) -> dict[str, Any]:
    if _ACTIVE_CHATS:
        raise HTTPException(status_code=409, detail="Agent 正在执行任务，请稍后再删除会话")
    sid = _clean_session_id(session_id)
    SESSIONS.pop(sid, None)
    save_sessions(SESSIONS)
    return {"ok": True, "session_id": sid}


@app.post("/api/answer")
def answer_ask(payload: AnswerRequest) -> dict[str, Any]:
    if not resolve_ask(payload.key, payload.answer):
        raise HTTPException(status_code=404, detail="没有找到等待中的 askuser 请求")
    return {"ok": True}


@app.post("/api/open")
def open_artifact(payload: OpenRequest) -> dict[str, Any]:
    workspace = get_workspace()
    target = Path(payload.path).expanduser()
    if not target.is_absolute():
        target = workspace / target
    target = target.resolve()
    if target != workspace and workspace not in target.parents:
        raise HTTPException(status_code=400, detail="只能打开当前工作区内的产物")
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"路径不存在：{payload.path}")
    opener = shutil.which("xdg-open") or shutil.which("open")
    if not opener:
        raise HTTPException(status_code=400, detail="系统没有找到 xdg-open/open，无法打开图形界面")
    try:
        subprocess.Popen(
            [opener, str(target)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "path": str(target)}


@app.get("/api/file")
def serve_workspace_file(path: str) -> FileResponse:
    """Serve an image (or small media) from the workspace for the show tool."""
    workspace = get_workspace()
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = workspace / target
    try:
        target = target.resolve()
    except OSError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if target != workspace and workspace not in target.parents:
        raise HTTPException(status_code=400, detail="只能读取工作区内的文件")
    if not target.is_file():
        raise HTTPException(status_code=404, detail=f"路径不存在：{path}")
    allowed = {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif",
        ".pdf", ".txt", ".md", ".csv", ".json", ".log",
    }
    if target.suffix.lower() not in allowed:
        raise HTTPException(status_code=415, detail="该类型不支持内嵌展示")
    if target.stat().st_size > 30 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="文件超过 30 MiB")
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return FileResponse(target, media_type=media_type, filename=target.name)


@app.post("/api/reset")
def reset_session(
    payload: ResetRequest | None = None, session_id: str | None = None
) -> dict[str, Any]:
    if _ACTIVE_CHATS:
        raise HTTPException(status_code=409, detail="Agent 正在执行任务，请稍后再清空会话")
    sid = _clean_session_id(payload.session_id if payload else session_id)
    SESSIONS[sid] = {
        "history": [],
        "messages": [],
        "plan": [],
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    save_sessions(SESSIONS)
    return {"session_id": sid, "ok": True}


def _sse_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }


def _stream_from_run(run: ActiveRun) -> StreamingResponse:
    async def event_stream():
        queue = run.subscribe()
        try:
            while True:
                item = await queue.get()
                yield _sse(item)
                if item.get("type") == "finish":
                    break
        finally:
            # Detach only — never cancel the worker on client disconnect.
            run.unsubscribe(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers=_sse_headers(),
    )


@app.post("/api/chat")
async def chat(payload: ChatRequest, request: Request) -> StreamingResponse:
    global _ACTIVE_CHATS
    sid = _clean_session_id(payload.session_id)
    session = get_or_create(SESSIONS, sid)
    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message 不能为空")

    _cleanup_stale_runs()
    existing = ACTIVE_RUNS.get(sid)
    if existing and existing.is_running():
        raise HTTPException(
            status_code=409,
            detail="该会话已有 Agent 任务正在运行，请等待完成或先停止",
        )
    if _ACTIVE_CHATS:
        raise HTTPException(status_code=409, detail="已有 Agent 请求正在运行，暂不能切换工作目录或发送消息")

    agent_message: str | list[UserContent]
    saved_paths: list[str] = []
    if payload.attachments:
        try:
            for attachment in payload.attachments:
                saved_paths.append(
                    str(_save_attachment(attachment).relative_to(get_workspace()))
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        model_info = describe_model(get_runtime_settings().get("model"))
        allow_vision = bool(model_info.get("vision"))
        agent_message = _build_user_prompt(
            message,
            payload.attachments,
            saved_paths,
            allow_vision=allow_vision,
        )
    else:
        agent_message = message

    # Display string for the chat bubble (never the binary payload).
    if isinstance(agent_message, list):
        display_user = message
        if saved_paths:
            display_user += "\n\n📎 " + "、".join(Path(p).name for p in saved_paths)
    else:
        display_user = agent_message
    session["messages"].append({"role": "user", "content": display_user})
    needs_title = (
        not str(session.get("title") or "").strip()
        or session.get("title") == "新对话"
    )
    if needs_title:
        session["title"] = generate_title(payload.message)
    session["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if len(session["messages"]) > SESSION_LIMIT:
        session["messages"] = session["messages"][-SESSION_LIMIT:]

    settings = get_runtime_settings()
    # Token estimate uses the display text (never the binary image payload).
    extra_for_tokens = display_user
    original_history = list(session.get("history", []))
    auto_compacted = False
    if original_history and should_compact(
        estimate_tokens(original_history, extra_for_tokens),
        int(settings["context_size"]),
        float(settings["compaction_threshold"]),
    ):
        original_history = compact_history(
            original_history, summarize_history(original_history)
        )
        auto_compacted = True
    session["history"] = original_history
    save_sessions(SESSIONS)

    run = ActiveRun(session_id=sid)
    ACTIVE_RUNS[sid] = run
    _ACTIVE_CHATS += 1

    async def run_worker() -> None:
        global _ACTIVE_CHATS
        ask_key: str | None = None
        try:
            history = _trim_history(list(session.get("history", [])))
            plan = session.setdefault("plan", [])
            run.publish(
                {
                    "type": "context_status",
                    "used": estimate_tokens(history, extra_for_tokens),
                    "total": int(settings["context_size"]),
                    "percent": round(
                        100
                        * estimate_tokens(history, extra_for_tokens)
                        / max(1, int(settings["context_size"])),
                        1,
                    ),
                    "auto_compacted": auto_compacted,
                }
            )
            ask_key = f"{sid}:{uuid.uuid4().hex}"
            new_history, current_messages = await stream_run(
                message=agent_message,
                history=history,
                plan=plan,
                events=_RunEventSink(run),
                ask_key=ask_key,
            )
            manual_compacted = any(
                isinstance(part, ToolCallPart) and part.tool_name == "compact_context"
                for message in current_messages
                if isinstance(message, ModelResponse)
                for part in message.parts
            )
            if manual_compacted:
                session["history"] = compact_history(
                    history + current_messages,
                    summarize_history(history + current_messages),
                )
            else:
                session["history"] = new_history

            # Final token estimate for this turn (UI also refreshes /api/health after done).
            final_history = session["history"]
            final_used = estimate_tokens(final_history)
            run.publish(
                {
                    "type": "context_status",
                    "used": final_used,
                    "total": int(settings["context_size"]),
                    "percent": round(
                        100 * final_used / max(1, int(settings["context_size"])),
                        1,
                    ),
                    "auto_compacted": auto_compacted or manual_compacted,
                }
            )

            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            for model_message in current_messages:
                if not hasattr(model_message, "parts"):
                    continue
                for part in model_message.parts:
                    if type(part).__name__ == "TextPart":
                        text_parts.append(getattr(part, "content", ""))
                    elif type(part).__name__ == "ThinkingPart":
                        reasoning_parts.append(getattr(part, "content", ""))
            text = text_parts[-1] if text_parts else ""
            # Event deltas are already streamed; this is for persistence.
            tools = _tools_from_history(current_messages)
            display = display_assistant_from_result(current_messages, text, tools)
            session["messages"].append(
                {
                    key: value
                    for key, value in display.items()
                    if key in {"role", "content", "reasoning", "tools", "blocks"}
                }
            )
            session["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            if len(session["messages"]) > SESSION_LIMIT:
                session["messages"] = session["messages"][-SESSION_LIMIT:]
            if needs_title:
                session["title"] = await generate_session_title(payload.message)
            session["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            save_sessions(SESSIONS)
            run.publish(
                {
                    "type": "done",
                    "session_id": sid,
                    "title": session.get("title") or "新对话",
                    "message": {
                        "role": "assistant",
                        "content": display.get("content", text),
                        "reasoning": display.get("reasoning", ""),
                    },
                }
            )
        except asyncio.CancelledError:
            # Keep any session mutations already written; finish event is published in finally.
            try:
                session["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                save_sessions(SESSIONS)
            except Exception:
                pass
            raise
        except Exception as exc:  # pragma: no cover - surfaced to the browser
            session.pop("_turn_tools", None)
            run.publish(
                {"type": "error", "message": str(exc) or type(exc).__name__}
            )
        finally:
            cancel_ask(ask_key)
            _ACTIVE_CHATS = max(0, _ACTIVE_CHATS - 1)
            run.publish({"type": "finish"})

    run.worker = asyncio.create_task(run_worker())
    return _stream_from_run(run)


class _RunEventSink:
    """Adapter so stream_run can publish into an ActiveRun."""

    def __init__(self, run: ActiveRun) -> None:
        self._run = run

    def put_nowait(self, event: dict[str, Any]) -> None:
        self._run.publish(event)


@app.get("/api/chat/stream")
async def chat_stream(session_id: str | None = None) -> StreamingResponse:
    """Reattach to an in-flight run after a page reload/navigation."""
    sid = _clean_session_id(session_id)
    run = _get_run(sid)
    if run is None or not run.is_running():
        raise HTTPException(status_code=404, detail="当前会话没有正在运行的任务")
    return _stream_from_run(run)


@app.get("/api/run")
def run_status(session_id: str | None = None) -> dict[str, Any]:
    sid = _clean_session_id(session_id)
    run = _get_run(sid)
    running = bool(run and run.is_running())
    return {
        "session_id": sid,
        "running": running,
        "event_count": len(run.events) if run else 0,
        "active_chats": _ACTIVE_CHATS,
    }


class StopRequest(BaseModel):
    session_id: str | None = None


@app.post("/api/run/stop")
async def stop_run(payload: StopRequest | None = None) -> dict[str, Any]:
    sid = _clean_session_id(payload.session_id if payload else None)
    run = _get_run(sid)
    if run is None or not run.is_running() or run.worker is None:
        return {"ok": True, "session_id": sid, "stopped": False}
    worker = run.worker
    worker.cancel()
    for _ in range(150):
        if worker.done():
            break
        await asyncio.sleep(0.1)
    return {"ok": True, "session_id": sid, "stopped": True}


@app.post("/api/llama/stop")
def stop_llama_server() -> dict[str, Any]:
    """Stop the main chat llama-server (port 8080) only.

    Also writes a manual-stop flag so the watchdog / systemd will NOT auto-start
    until the user explicitly calls /api/llama/start.
    """
    if _ACTIVE_CHATS:
        raise HTTPException(
            status_code=409,
            detail="Agent 正在执行任务，请先停止任务再关闭模型服务",
        )

    # 1) Mark intentional stop FIRST (watchdog checks this before restart).
    _set_llama_manual_stop(True)

    # 2) Stop the systemd unit so Restart=always cannot resurrect it.
    unit = _systemd_user_unit("stop", "spark-llama.service", timeout=35)

    # 3) Kill any leftover listener / pid-file process on :8080.
    pid_file = ROOT_DIR / ".run" / "llama.pid"
    killed: list[int] = []
    if pid_file.exists():
        try:
            killed.append(int(pid_file.read_text().strip()))
        except (OSError, ValueError):
            pass
    for pid in _pids_on_port(LLAMA_PORT):
        if pid not in killed:
            killed.append(pid)
    for pid in killed:
        if pid:
            _terminate_pid(pid, timeout=2.5)
    # Wait for port to free
    for _ in range(50):
        if not _pids_on_port(LLAMA_PORT):
            break
        time.sleep(0.1)
    try:
        pid_file.unlink(missing_ok=True)
    except OSError:
        pass
    _HEALTH_CACHE["at"] = 0.0
    _HEALTH_CACHE["data"] = None
    still = _pids_on_port(LLAMA_PORT)
    return {
        "ok": not still,
        "stopped_pids": [p for p in killed if p],
        "port": LLAMA_PORT,
        "still_running": still,
        "manual_stop": True,
        "unit": unit,
        "message": "已手动停止模型（不会自动重启；按钮变为「启动模型」）"
        if not still
        else "仍有进程占用端口",
    }


@app.post("/api/llama/start")
def start_llama_server() -> dict[str, Any]:
    """Start main llama after a manual stop (clears the no-autostart flag)."""
    if _ACTIVE_CHATS:
        raise HTTPException(status_code=409, detail="Agent 正在执行任务，请稍后再启动模型")

    # Clear flag first so watchdog will not race us.
    _set_llama_manual_stop(False)

    # If something already healthy, just report success.
    try:
        payload = _fetch_json(f"http://127.0.0.1:{LLAMA_PORT}/health", timeout=1.0)
        if isinstance(payload, dict) and payload.get("status") == "ok":
            _HEALTH_CACHE["at"] = 0.0
            _HEALTH_CACHE["data"] = None
            return {
                "ok": True,
                "already_running": True,
                "manual_stop": False,
                "message": "模型已在运行",
            }
    except Exception:
        pass

    # Prefer systemd unit (reads settings.json via spark-llama-start.sh).
    # restart (not start): unit may be parked in `sleep infinity` while flag was set.
    unit = _systemd_user_unit("restart", "spark-llama.service", timeout=15)
    if not unit.get("ok"):
        # Fallback: spawn directly from settings.
        settings = get_runtime_settings()
        try:
            restarted = _restart_llama(settings)
        except HTTPException as exc:
            raise HTTPException(
                status_code=500,
                detail=f"启动失败：systemd={unit.get('stderr') or unit.get('error')}；{exc.detail}",
            ) from exc
        _HEALTH_CACHE["at"] = 0.0
        _HEALTH_CACHE["data"] = None
        return {
            "ok": True,
            "already_running": False,
            "manual_stop": False,
            "unit": unit,
            "llama": restarted,
            "message": "模型已启动（直接拉起）",
        }

    # Wait until healthy (model load can take tens of seconds).
    deadline = time.monotonic() + 90
    last_err = ""
    while time.monotonic() < deadline:
        try:
            payload = _fetch_json(f"http://127.0.0.1:{LLAMA_PORT}/health", timeout=1.5)
            if isinstance(payload, dict) and payload.get("status") == "ok":
                _HEALTH_CACHE["at"] = 0.0
                _HEALTH_CACHE["data"] = None
                return {
                    "ok": True,
                    "already_running": False,
                    "manual_stop": False,
                    "unit": unit,
                    "message": "模型已启动并就绪",
                }
        except Exception as exc:  # noqa: BLE001
            last_err = str(exc)
        time.sleep(0.5)

    raise HTTPException(
        status_code=504,
        detail=f"等待模型启动超时：{last_err or unit.get('stderr') or 'unknown'}",
    )


def _serve_html(filename: str) -> HTMLResponse:
    """Serve an HTML page with cache-busting asset URLs from file mtimes.

    Browsers that already cached an old index.html still lose on no-store;
    new deploys change ?v= even if something kept a stale copy.
    """
    path = STATIC_DIR / filename
    html = path.read_text(encoding="utf-8")
    try:
        css_v = (STATIC_DIR / "styles.css").stat().st_mtime_ns
        js_v = (STATIC_DIR / "app.js").stat().st_mtime_ns
    except OSError:
        css_v = js_v = 0
    html = re.sub(r"(styles\.css\?v=)[^\"]+", rf"\g<1>{css_v}", html)
    html = re.sub(r"(app\.js\?v=)[^\"]+", rf"\g<1>{js_v}", html)
    # Per-page scripts get their own mtime token.
    for asset in ():
        try:
            asset_v = (STATIC_DIR / asset).stat().st_mtime_ns
            html = re.sub(rf"({re.escape(asset)}\?v=)[^\"]+", rf"\g<1>{asset_v}", html)
        except OSError:
            pass
    return HTMLResponse(
        html,
        headers={
            "Cache-Control": "no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/")
def index() -> HTMLResponse:
    return _serve_html("index.html")


@app.get("/about")
def about() -> HTMLResponse:
    return _serve_html("about.html")


@app.get("/settings")
def settings_page() -> HTMLResponse:
    return _serve_html("settings.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT)
