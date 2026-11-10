from __future__ import annotations

import json
import os
import re
import shutil
import struct
import threading
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
# Default GGUF search dir — override via SPARK_MODEL_DIR or settings.model_dir
MODEL_PATH = Path(os.getenv("SPARK_MODEL", str(Path.home() / "models" / "model.gguf")))
_default_model_dir = os.getenv("SPARK_MODEL_DIR") or str(MODEL_PATH.parent)
MODEL_DIR = Path(_default_model_dir).expanduser()
if not MODEL_DIR.is_absolute():
    MODEL_DIR = (ROOT_DIR / MODEL_DIR).resolve()
else:
    MODEL_DIR = MODEL_DIR.resolve()
LLAMA_PORT = int(os.getenv("SPARK_LLAMA_PORT", "8080"))
WEB_PORT = int(os.getenv("SPARK_WEB_PORT", "8787"))
WEB_HOST = os.getenv("SPARK_WEB_HOST", "127.0.0.1")
LLAMA_BASE_URL = os.getenv(
    "SPARK_LLAMA_BASE_URL", f"http://127.0.0.1:{LLAMA_PORT}/v1"
).rstrip("/")
# Fixed OpenAI-facing alias so the agent client keeps working across GGUF swaps.
MODEL_NAME = os.getenv("SPARK_MODEL_NAME", "Spark")
CONTEXT_SIZE = int(os.getenv("SPARK_CONTEXT_SIZE", "65536"))
GPU_LAYERS = os.getenv("SPARK_GPU_LAYERS", "18")
TEMPERATURE = float(os.getenv("SPARK_TEMPERATURE", "0.2"))
MAX_TOKENS = int(os.getenv("SPARK_MAX_TOKENS", "12288"))
TOOL_TIMEOUT = float(os.getenv("SPARK_TOOL_TIMEOUT", "35"))
MAX_READ_BYTES = int(os.getenv("SPARK_MAX_READ_BYTES", str(256 * 1024)))
MAX_TOOL_OUTPUT = int(os.getenv("SPARK_MAX_TOOL_OUTPUT", "12000"))
SESSION_LIMIT = int(os.getenv("SPARK_SESSION_LIMIT", "100"))
HISTORY_LIMIT = int(os.getenv("SPARK_HISTORY_LIMIT", "60"))

RUN_DIR = ROOT_DIR / ".run"
SESSION_FILE = RUN_DIR / "sessions.json"
WORKSPACE_STATE_FILE = RUN_DIR / "workspace.json"
SETTINGS_FILE = RUN_DIR / "settings.json"
RUN_DIR.mkdir(parents=True, exist_ok=True)

_SETTINGS_LOCK = threading.RLock()
# Empty string means “auto / use env default”.
_ALLOWED_SETTINGS = {
    "context_size": {16384, 32768, 65536, 131072},
    "backend_profile": {"gpu", "ram", "auto"},
    "kv_cache": {"q8_0", "q4_0", "f16"},
    "gpu_layers": {"auto", "999", "36", "18", "0"},
    "threads": set(range(1, 33)),
    "threads_batch": set(range(1, 33)),
    "flash_attention": {"on", "off", "auto"},
    "auto_compaction": {True, False},
    "max_tokens": {4096, 8192, 12288, 16384},
    # Capability toggles (only apply when the GGUF actually has the feature).
    "enable_mtp": {True, False},
    "enable_vision": {True, False},
    "enable_speculative": {True, False},
    # Free-form paths: llama-server binary + GGUF scan directory.
    "llama_bin": {str},
    "model_dir": {str},
}
DEFAULT_RUNTIME_SETTINGS = {
    "context_size": 65536,
    "backend_profile": "ram",
    "kv_cache": "q4_0",
    "gpu_layers": "18",
    "threads": 8,
    "threads_batch": 8,
    "flash_attention": "on",
    "auto_compaction": True,
    "compaction_threshold": 0.80,
    "max_tokens": 12288,
    "model": str(MODEL_PATH),
    "enable_mtp": True,
    "enable_vision": True,
    "enable_speculative": False,
    "llama_bin": os.getenv("SPARK_LLAMA_BIN", ""),
    "model_dir": os.getenv("SPARK_MODEL_DIR", str(MODEL_DIR)),
}


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{num_bytes} B"


def find_llama_server(explicit: str = "") -> str | None:
    """Locate llama-server: explicit setting → env → PATH → common install paths."""
    candidates: list[str] = []
    for value in (
        explicit,
        os.getenv("SPARK_LLAMA_BIN", ""),
        shutil.which("llama-server") or "",
        "/usr/bin/llama-server",
        "/usr/local/bin/llama-server",
        "/opt/llama.cpp/build/bin/llama-server",
        str(Path.home() / "llama.cpp/build/bin/llama-server"),
        str(Path.home() / ".local/bin/llama-server"),
    ):
        if value and value not in candidates:
            candidates.append(value)
    for cand in candidates:
        path = Path(cand).expanduser()
        try:
            if path.is_file() and os.access(path, os.X_OK):
                return str(path.resolve())
        except OSError:
            continue
    return None


def get_model_dir() -> Path:
    """Active GGUF scan directory (settings.model_dir or env/default)."""
    try:
        raw = str(get_runtime_settings().get("model_dir") or "").strip()
    except Exception:
        raw = ""
    if raw:
        return Path(raw).expanduser().resolve()
    return MODEL_DIR


def resolve_model_file(value: str | Path) -> Path:
    """Resolve a GGUF path (absolute or filename under model_dir) with safety checks."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("模型路径不能为空")
    model_dir = get_model_dir()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = model_dir / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"模型文件不存在：{candidate}") from exc
    if resolved.suffix.lower() != ".gguf":
        raise ValueError(f"只支持 .gguf 文件：{resolved.name}")
    if not resolved.is_file():
        raise ValueError(f"不是文件：{resolved}")
    try:
        resolved.relative_to(model_dir)
    except ValueError as exc:
        raise ValueError(f"模型文件必须位于模型目录内：{model_dir}") from exc
    if "mmproj" in resolved.name.lower():
        raise ValueError(f"这是多模态投影文件，不是对话模型：{resolved.name}")
    return resolved


def describe_model(path_value: str | Path | None = None) -> dict[str, Any]:
    """Filesystem + capability metadata for a GGUF model."""
    try:
        resolved = resolve_model_file(path_value or get_runtime_settings()["model"])
    except ValueError as exc:
        return {
            "path": str(path_value or ""),
            "name": MODEL_NAME,
            "filename": "",
            "size_bytes": 0,
            "size_human": "—",
            "quant": "",
            "error": str(exc),
        }
    stat = resolved.stat()
    stem = resolved.stem
    quant = ""
    for tag in ("Q8_0", "Q4_K_M", "Q4_0", "Q4_K_S", "Q5_K_M", "Q5_0", "IQ4_XS", "IQ4_NL", "F16", "BF16"):
        if tag.lower() in stem.lower():
            quant = tag
            break
    caps = model_capabilities(resolved)
    mmproj = Path(caps["mmproj"]) if caps.get("mmproj") else find_mmproj_for(resolved)
    return {
        "path": str(resolved),
        "name": stem,
        "filename": resolved.name,
        "size_bytes": stat.st_size,
        "size_human": human_size(stat.st_size),
        "quant": quant,
        "architecture": caps.get("architecture") or "",
        "vision": bool(caps.get("vision")),
        "mtp": bool(caps.get("mtp")),
        "diffusion": bool(caps.get("diffusion")),
        "embedding": bool(caps.get("embedding")),
        "reranker": bool(caps.get("reranker")),
        "text": bool(caps.get("text")),
        "chat": bool(caps.get("chat")),
        "capabilities": caps.get("capabilities") or [],
        "labels": caps.get("labels") or [],
        "mtp_tensors": caps.get("mtp_tensors") or [],
        "mtp_enabled": caps.get("mtp_enabled"),
        "vision_enabled": caps.get("vision_enabled"),
        "mmproj": str(mmproj) if mmproj else None,
        "mmproj_size_human": human_size(mmproj.stat().st_size) if mmproj else None,
        "error": None,
    }


def list_gguf_models() -> list[dict[str, Any]]:
    """List chat-capable GGUF files in the configured model_dir (excludes mmproj)."""
    directory = get_model_dir()
    if not directory.is_dir():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.gguf"), key=lambda p: p.name.lower()):
        if "mmproj" in path.name.lower():
            continue
        try:
            info = describe_model(path)
        except OSError:
            continue
        if info.get("error"):
            continue
        info["selected"] = False
        items.append(info)
    return items


QUANT_TAGS = (
    "Q2_K", "Q3_K_M", "Q4_K_S", "Q4_K_M", "Q4_0", "Q4_1", "Q5_K_S", "Q5_K_M",
    "Q6_K", "Q8_0", "Q8_K", "IQ4_XS", "IQ4_NL", "IQ3_XXS", "BF16", "F16", "F32",
)


def _base_name(stem: str) -> str:
    """Normalize a GGUF stem: drop the mmproj token and quant tags.

    ``Qwen3VL-2B-Instruct-Q4_K_M`` and ``mmproj-Qwen3VL-2B-Instruct-Q8_0``
    both reduce to ``qwen3vl2binstruct`` so they can be paired.
    """
    low = stem.lower()
    low = low.replace("mmproj", "")
    for tag in QUANT_TAGS:
        low = low.replace(tag.lower(), "")
    return re.sub(r"[^a-z0-9]+", "", low)


def find_mmproj_for(model_path: Path) -> Path | None:
    """Locate a sibling vision projector.

    Supports both naming styles:
      mmproj-<model>-<quant>.gguf   (Qwen3-VL)
      <model>-mmproj-<quant>.gguf   （视觉投影文件命名风格）
    """
    directory = model_path.parent
    base = _base_name(model_path.stem)
    if not base:
        return None
    candidates: list[Path] = []
    for path in directory.glob("*.gguf"):
        if path == model_path or "mmproj" not in path.name.lower():
            continue
        if _base_name(path.stem) == base:
            candidates.append(path)
    if not candidates:
        return None
    # Prefer smaller projector (Q8_0 / bf16 order by size).
    try:
        return min(candidates, key=lambda p: p.stat().st_size)
    except OSError:
        return candidates[0]


def read_gguf_architecture(path: Path) -> str:
    """Read general.architecture from GGUF KV without extra deps."""
    meta = read_gguf_meta(path)
    return str(meta.get("architecture") or "")


_DIFFUSION_ARCH_HINTS = (
    "diffusion", "flux", "sdxl", "sd3", "stable", "cascade", "lumina",
    "hidream", "kolors", "wan", "hunyuanvideo", "cogvideo", "mmdit",
)
_TEXT_ARCH_HINTS = (
    "llama", "qwen", "spark", "mimo", "gemma", "phi", "mistral", "gpt",
    "granite", "intern", "yi", "glm", "cohere", "command",
)


def read_gguf_meta(path: Path, *, scan_tensors: bool = True) -> dict[str, Any]:
    """Read architecture + capability signals from a GGUF header.

    Returns keys: architecture, mtp, vision_arch, diffusion, embedding,
    reranker, nextn_tensors (sample), tensor_count.
    """
    info: dict[str, Any] = {
        "architecture": "",
        "mtp": False,
        "vision_arch": False,
        "diffusion": False,
        "embedding": False,
        "reranker": False,
        "nextn_tensors": [],
        "tensor_count": 0,
        "error": None,
    }
    try:
        with path.open("rb") as fh:
            if fh.read(4) != b"GGUF":
                info["error"] = "not a GGUF file"
                return info

            fh.read(4)  # version
            n_tensors = struct.unpack("<Q", fh.read(8))[0]
            n_kv = struct.unpack("<Q", fh.read(8))[0]
            info["tensor_count"] = int(n_tensors)

            def rstr() -> str:
                n = struct.unpack("<Q", fh.read(8))[0]
                return fh.read(n).decode("utf-8", "replace")

            sizes = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
            for _ in range(n_kv):
                key = rstr()
                vtype = struct.unpack("<I", fh.read(4))[0]
                if vtype == 8:  # string
                    value = rstr()
                    if key == "general.architecture":
                        info["architecture"] = value
                    elif key.endswith(".pooling_type") and value not in ("", "none", "0"):
                        info["embedding"] = True
                    elif "clip" in key or "vision" in key or key.endswith(".mmproj"):
                        info["vision_arch"] = True
                    continue
                if vtype == 9:  # array
                    elem = struct.unpack("<I", fh.read(4))[0]
                    count = struct.unpack("<Q", fh.read(8))[0]
                    if elem == 8:
                        for _ in range(count):
                            rstr()
                    else:
                        fh.read(sizes.get(elem, 4) * count)
                    continue
                fh.read(sizes.get(vtype, 4))

            if scan_tensors and n_tensors:
                nextn: list[str] = []
                diff_hits = 0
                for _ in range(min(n_tensors, 8000)):
                    name = rstr()
                    n_dims = struct.unpack("<I", fh.read(4))[0]
                    for _ in range(n_dims):
                        struct.unpack("<Q", fh.read(8))[0]
                    struct.unpack("<I", fh.read(4))[0]  # dtype
                    struct.unpack("<Q", fh.read(8))[0]  # offset
                    low = name.lower()
                    if "nextn" in low or low.endswith(".mtp") or ".mtp." in low:
                        if len(nextn) < 8:
                            nextn.append(name)
                    if any(
                        token in low
                        for token in (
                            "vae.", "unet.", "diffusion_model.", "clip.",
                            "text_encoder", "encoder_embedding.",
                        )
                    ):
                        diff_hits += 1
                info["nextn_tensors"] = nextn
                if nextn:
                    info["mtp"] = True
                if diff_hits >= 2:
                    info["diffusion"] = True
    except (OSError, struct.error, UnicodeDecodeError) as exc:
        info["error"] = str(exc)
        return info

    arch = (info["architecture"] or "").lower()
    if any(h in arch for h in _DIFFUSION_ARCH_HINTS):
        info["diffusion"] = True
    if any(h in arch for h in ("clip", "siglip", "qwen2vl", "qwen3vl", "mimovl", "internvl", "llava")):
        info["vision_arch"] = True
    if "rerank" in arch or arch.endswith("-reranker"):
        info["reranker"] = True
    if arch.startswith("bert") or arch in {"nomic-bert", "jina-bert"}:
        info["embedding"] = True
    return info


def model_capabilities(path_value: str | Path | None = None) -> dict[str, Any]:
    """Structured capability report for UI + runtime flags."""
    try:
        resolved = resolve_model_file(path_value or get_runtime_settings()["model"])
    except ValueError as exc:
        return {
            "path": str(path_value or ""),
            "name": "",
            "vision": False,
            "mtp": False,
            "diffusion": False,
            "embedding": False,
            "reranker": False,
            "text": False,
            "capabilities": [],
            "labels": [],
            "error": str(exc),
        }
    meta = read_gguf_meta(resolved)
    mmproj = find_mmproj_for(resolved)
    vision = bool(mmproj) or bool(meta.get("vision_arch")) and not meta.get("diffusion")
    # Vision chat needs mmproj for llama.cpp multimodal; arch-only is a weak signal.
    vision = bool(mmproj)
    mtp = bool(meta.get("mtp"))
    diffusion = bool(meta.get("diffusion"))
    embedding = bool(meta.get("embedding"))
    reranker = bool(meta.get("reranker"))
    text = not diffusion and not embedding and not reranker
    caps: list[str] = []
    labels: list[str] = []
    if text:
        caps.append("text")
        labels.append("文本")
    if vision:
        caps.append("vision")
        labels.append("视觉")
    if mtp:
        caps.append("mtp")
        labels.append("MTP")
    if diffusion:
        caps.append("diffusion")
        labels.append("扩散")
    if embedding:
        caps.append("embedding")
        labels.append("Embedding")
    if reranker:
        caps.append("reranker")
        labels.append("Reranker")
    settings = get_runtime_settings()
    return {
        "path": str(resolved),
        "name": resolved.stem,
        "filename": resolved.name,
        "architecture": meta.get("architecture") or "",
        "vision": vision,
        "mmproj": str(mmproj) if mmproj else None,
        "mtp": mtp,
        "mtp_tensors": meta.get("nextn_tensors") or [],
        "diffusion": diffusion,
        "embedding": embedding,
        "reranker": reranker,
        "text": text,
        "chat": text or vision,
        "capabilities": caps,
        "labels": labels,
        # Current preference vs actual support (for UI "will apply" hints).
        "mtp_enabled": bool(settings.get("enable_mtp", True)) and mtp,
        "vision_enabled": bool(settings.get("enable_vision", True)) and vision,
        "speculative_enabled": bool(settings.get("enable_speculative", False)),
        "error": meta.get("error"),
    }


def _coerce_setting(key: str, value: Any) -> Any:
    if key in {"context_size", "threads", "threads_batch", "max_tokens"}:
        value = int(value)
    elif key == "compaction_threshold":
        value = float(value)
    elif key in {"auto_compaction", "enable_mtp", "enable_vision", "enable_speculative"}:
        if isinstance(value, str):
            value = value.strip().lower() in {"1", "true", "yes", "on"}
        else:
            value = bool(value)
    elif key == "llama_bin":
        text = str(value or "").strip()
        if text:
            p = Path(text).expanduser()
            if not p.is_file():
                raise ValueError(f"llama-server 不存在：{text}")
            text = str(p.resolve())
        return text
    elif key == "model_dir":
        text = str(value or "").strip()
        if not text:
            return str(MODEL_DIR)
        p = Path(text).expanduser()
        if not p.is_dir():
            raise ValueError(f"模型目录不存在：{text}")
        return str(p.resolve())
    elif key == "model":
        return str(resolve_model_file(value))
    if key == "compaction_threshold":
        if not 0.50 <= value <= 0.95:
            raise ValueError("compaction_threshold 必须在 0.50 到 0.95 之间")
        return value
    allowed = _ALLOWED_SETTINGS.get(key)
    # Free-form string keys use allowed={str} and are handled above.
    if isinstance(allowed, set) and str not in allowed and value not in allowed:
        raise ValueError(f"{key} 的值不受支持：{value}")
    return value


def get_runtime_settings() -> dict[str, Any]:
    settings = dict(DEFAULT_RUNTIME_SETTINGS)
    if SETTINGS_FILE.exists():
        try:
            raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = None
        if isinstance(raw, dict):
            for key, value in raw.items():
                if key not in DEFAULT_RUNTIME_SETTINGS:
                    continue
                try:
                    settings[key] = _coerce_setting(key, value)
                except (ValueError, TypeError, OSError):
                    # Keep the default for a single bad key (e.g. deleted GGUF).
                    continue
    return settings


def set_runtime_settings(values: dict[str, Any]) -> dict[str, Any]:
    settings = get_runtime_settings()
    for key, value in values.items():
        if key not in DEFAULT_RUNTIME_SETTINGS:
            raise ValueError(f"未知设置：{key}")
        settings[key] = _coerce_setting(key, value)
    with _SETTINGS_LOCK:
        SETTINGS_FILE.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return settings

_WORKSPACE_LOCK = threading.RLock()


def _resolve_workspace(value: str | Path) -> Path:
    path = Path(value).expanduser().resolve()
    if path == Path("/"):
        raise ValueError("不能把根目录 / 设为工作区")
    if not path.is_dir():
        raise ValueError(f"工作区不存在或不是目录：{path}")
    return path


def _load_workspace() -> Path:
    env_value = os.getenv("SPARK_WORKSPACE")
    if env_value:
        return _resolve_workspace(env_value)
    if WORKSPACE_STATE_FILE.exists():
        try:
            raw = json.loads(WORKSPACE_STATE_FILE.read_text(encoding="utf-8"))
            saved = raw.get("path") if isinstance(raw, dict) else None
            if saved:
                return _resolve_workspace(saved)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    return ROOT_DIR


WORKSPACE = _load_workspace()


def get_workspace() -> Path:
    """Return the active workspace directory."""
    with _WORKSPACE_LOCK:
        return WORKSPACE


def set_workspace(value: str | Path) -> Path:
    """Switch and persist the active workspace directory."""
    path = _resolve_workspace(value)
    with _WORKSPACE_LOCK:
        global WORKSPACE
        WORKSPACE = path
        WORKSPACE_STATE_FILE.write_text(
            json.dumps({"path": str(path)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return path


# auto: use bubblewrap if installed; on: require it; off: execute directly.
SHELL_SANDBOX = os.getenv("SPARK_SHELL_SANDBOX", "auto").lower()
ALLOW_NETWORK_IN_SHELL = os.getenv("SPARK_SHELL_NETWORK", "1") == "1"


def system_prompt(workspace: str | Path | None = None) -> str:
    active_workspace = Path(workspace).expanduser().resolve() if workspace else get_workspace()
    return f"""你是 Spark，一个运行在本机的轻量级编码 Agent。

工作区：{active_workspace}

行为要求：
- 默认使用简体中文回答；用户使用其他语言时跟随用户。
- 回答要简洁、具体，不编造文件内容或命令结果。
- 需要查看或修改工作区文件时，优先使用工具，不要凭记忆猜测。
- 对多步骤任务先调用 plan；执行过程中根据结果更新计划。
- 分析 Word/PowerPoint/Excel/PDF 等附件时优先使用 office（结构化读写表格/文档）或 analyze；不要把二进制文件内容直接当作文本猜测。
- 若用户消息里直接附带了图片（多模态输入），请直接看图回答/定位，不要只依赖路径。
- 需要向用户展示内容时使用 show：
  - image：图片路径或 URL
  - url：网页链接（用户可点击进入）
  - table：表格，target 为 JSON 数组的行对象数组，例如 [{{"列A":1,"列B":2}}]
  - mindmap：思维导图，target 为 JSON 树 {{"label":"中心","children":[...]}}
  - file / text：工作区文件或短文本
  不要只在正文里干巴巴地贴路径。
- 查看目录优先使用 ls；删除文件或目录优先使用 del，并先 dry_run 预览，用户确认后再 confirm 执行。
- 遇到需要用户选择、确认或补充信息的歧义时，调用 askuser，并等待用户回答后再继续。
- 需要联网查找公开资料时使用 web_search；需要主动压缩旧上下文时使用 compact_context。
- 修改文本优先使用 edit，并先用 read/grep 获取准确原文；重要修改可先用 diff 预览 unified diff；批量改名和目录整理必须先 dry_run 预览，用户确认后再 confirm 执行。
- write 只允许写入工作区；shell 只在工作区可写，其余文件系统通常只读。
- shell 工具默认处于 bubblewrap 隔离环境。若命令因缺少挂载或网络而失败，请解释并尝试工作区内可行的方案。
- 工具返回错误时，读取错误并调整，不要声称操作成功。
- 每次完成用户交给你的任务后，最后一段必须以“## 任务总结”开头，并简洁列出：
  1. 任务目标；
  2. 实际执行的动作（包括调用过的工具）；
  3. 结果与验证情况；
  4. 文件/目录变更（没有则写“无”）；
  5. 风险或后续建议（没有则写“无”）。
- 即使任务很简单也要给出任务总结；若用户明确要求只输出某种特定格式，则以用户的明确要求优先。
- 最终答案应说明关键结论；只有确有必要时才展示简短命令或代码。
"""


SYSTEM_PROMPT = system_prompt()
