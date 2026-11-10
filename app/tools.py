from __future__ import annotations

import base64
import difflib
import fnmatch
import html as html_lib
import os
import re
import shutil
import subprocess
import zipfile
from datetime import datetime
from urllib.parse import parse_qs, quote, unquote, urlparse
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .config import (
    ALLOW_NETWORK_IN_SHELL,
    MAX_READ_BYTES,
    MAX_TOOL_OUTPUT,
    SHELL_SANDBOX,
    TOOL_TIMEOUT,
    get_workspace,
)

_EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".run",
}


def _clip(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[输出已截断，共 {len(text)} 字符]"


def _safe_path(path: str, *, allow_missing: bool = False) -> Path:
    workspace = get_workspace()
    raw = str(path or ".").strip() or "."
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = workspace / candidate
    resolved = candidate.expanduser().resolve(strict=False)
    if resolved != workspace and workspace not in resolved.parents:
        raise ValueError(f"路径越界，只允许访问工作区：{workspace}")
    if not allow_missing and not resolved.exists():
        raise FileNotFoundError(f"文件或目录不存在：{resolved}")
    return resolved


def _shell_argv(command: str) -> list[str]:
    workspace = get_workspace()
    if SHELL_SANDBOX == "off" or not shutil.which("bwrap"):
        if SHELL_SANDBOX == "on":
            raise RuntimeError("已要求启用 bubblewrap，但系统中未找到 bwrap")
        return ["/bin/bash", "-lc", command]

    argv = [
        "bwrap",
        "--ro-bind",
        "/",
        "/",
        "--bind",
        str(workspace),
        str(workspace),
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--unshare-pid",
        "--die-with-parent",
    ]
    if not ALLOW_NETWORK_IN_SHELL:
        argv.append("--unshare-net")
    argv += [
        "--chdir",
        str(workspace),
        "/bin/bash",
        "-lc",
        command,
    ]
    return argv


def _walk_files(root: Path, pattern: str | None) -> list[Path]:
    files: list[Path] = []
    if root.is_file():
        return [root]
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in dirnames
            if name not in _EXCLUDED_DIRS and not name.startswith(".")
        ]
        for filename in filenames:
            path = Path(current) / filename
            if pattern and not fnmatch.fnmatch(path.name, pattern):
                continue
            files.append(path)
    return files


def read_text_file(path: str, start_line: int = 1, end_line: int | None = None) -> str:
    """读取工作区内的 UTF-8 文本文件，可选行号范围。

    Args:
        path: 相对于工作区的文件路径，也允许工作区内绝对路径。
        start_line: 起始行号，从 1 开始。
        end_line: 结束行号（含）；省略时读取到文件末尾。
    """
    try:
        target = _safe_path(path)
        if target.is_dir():
            raise IsADirectoryError(f"这是目录而不是文件：{target}")
        if target.stat().st_size > MAX_READ_BYTES:
            raise ValueError(
                f"文件超过单次读取上限 {MAX_READ_BYTES} 字节，请指定更小范围或先用 grep 定位"
            )
        text = target.read_text(encoding="utf-8")
    except Exception as exc:
        return f"ERROR: {exc}"

    lines = text.splitlines()
    start = max(1, int(start_line))
    stop = len(lines) if end_line is None else min(len(lines), int(end_line))
    if stop < start:
        return f"ERROR: 行范围无效：{start_line}-{end_line}"
    selected = lines[start - 1 : stop]
    numbered = "\n".join(
        f"{index:>6}\t{line}"
        for index, line in enumerate(selected, start=start)
    )
    return _clip(numbered or "(空文件)")


def grep_files(
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    case_sensitive: bool = False,
    max_results: int = 80,
) -> str:
    """在工作区文本文件中按正则表达式搜索。

    Args:
        pattern: Python 正则表达式。
        path: 工作区内文件或目录。
        glob: 可选文件名通配符，例如 *.py。
        case_sensitive: 是否区分大小写，默认不区分。
        max_results: 最大返回匹配行数，默认 80。
    """
    try:
        root = _safe_path(path)
        flags = 0 if case_sensitive else re.IGNORECASE
        rx = re.compile(pattern, flags)
    except Exception as exc:
        return f"ERROR: {exc}"

    results: list[str] = []
    truncated = False
    workspace = get_workspace()
    for file_path in _walk_files(root, glob):
        try:
            file_path = _safe_path(str(file_path))
            if file_path.stat().st_size > 2 * 1024 * 1024:
                continue
            text = file_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError, PermissionError):
            continue
        try:
            relative = file_path.relative_to(workspace)
        except ValueError:
            relative = file_path
        for number, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                results.append(f"{relative}:{number}:{line}")
                if len(results) >= int(max_results):
                    truncated = True
                    break
        if truncated:
            break
    if not results:
        return "没有匹配结果。"
    suffix = "\n…[结果已截断]" if truncated else ""
    return _clip("\n".join(results) + suffix)


def write_text_file(path: str, content: str, append: bool = False) -> Any:
    """创建或更新工作区内的 UTF-8 文本文件。

    Args:
        path: 相对于工作区的目标文件路径。
        content: 要写入的完整文本。
        append: 是否追加而不是覆盖，默认覆盖。
    """
    try:
        if len(content.encode("utf-8")) > 1024 * 1024:
            raise ValueError("单次写入不能超过 1 MiB")
        target = _safe_path(path, allow_missing=True)
        workspace = get_workspace()
        if target.exists() and target.is_dir():
            raise IsADirectoryError(f"目标是目录：{target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = ""
        if target.exists() and target.is_file():
            existing = target.read_text(encoding="utf-8")
        updated = existing + content if append else content
        mode = "a" if append else "w"
        with target.open(mode, encoding="utf-8", newline="") as handle:
            handle.write(content)
        diff = _clip(
            "\n".join(
                difflib.unified_diff(
                    existing.splitlines(),
                    updated.splitlines(),
                    fromfile="old",
                    tofile=_relative_label(target),
                    lineterm="",
                    n=3,
                )
            )
        )
        action = "追加到" if append else "写入"
        return {
            "ok": True,
            "message": f"成功{action} {_relative_label(target)}（{len(content.encode('utf-8'))} 字节）",
            "path": _relative_label(target),
            "diff": diff,
        }
    except Exception as exc:
        return f"ERROR: {exc}"


def run_shell(command: str, timeout: int = 30) -> str:
    """在隔离的工作区 shell 中执行命令并返回输出。

    Args:
        command: 要执行的 Bash 命令。
        timeout: 超时秒数，默认 30，最大 120。
    """
    timeout = max(1, min(int(timeout), 120))
    try:
        proc = subprocess.run(
            _shell_argv(command),
            cwd=get_workspace(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: 命令超过 {timeout} 秒，已终止。"
    except Exception as exc:
        return f"ERROR: {exc}"

    chunks = [f"$ {command}", f"[exit={proc.returncode}]"]
    if proc.stdout:
        chunks.append(proc.stdout.rstrip())
    if proc.stderr:
        chunks.append("[stderr]\n" + proc.stderr.rstrip())
    return _clip("\n".join(chunks))


def _relative_label(path: Path) -> str:
    try:
        return str(path.relative_to(get_workspace()))
    except ValueError:
        return str(path)


def _zip_text_members(archive: zipfile.ZipFile, names: list[str]) -> list[str]:
    texts: list[str] = []
    for name in names:
        try:
            root = ET.fromstring(archive.read(name))
        except (KeyError, ET.ParseError, OSError):
            continue
        values = [
            (element.text or "").strip()
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] in {"t", "text"}
        ]
        text = " ".join(value for value in values if value)
        if text:
            texts.append(text)
    return texts


def _natural_key(value: str) -> list[Any]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)]


def _analyze_docx(archive: zipfile.ZipFile, detail: str) -> str:
    texts = _zip_text_members(archive, ["word/document.xml"])
    limit = 400 if detail == "full" else 120
    paragraphs = [line.strip() for line in " ".join(texts).splitlines() if line.strip()]
    return "\n".join(paragraphs[:limit]) or "(未提取到正文)"


def _analyze_pptx(archive: zipfile.ZipFile, detail: str) -> str:
    names = sorted(
        (
            name
            for name in archive.namelist()
            if name.startswith("ppt/slides/slide") and name.endswith(".xml")
        ),
        key=_natural_key,
    )
    limit = 30 if detail == "full" else 12
    lines: list[str] = []
    for index, name in enumerate(names[:limit], start=1):
        texts = _zip_text_members(archive, [name])
        lines.append(f"[幻灯片 {index}] " + " / ".join(texts))
    return "\n".join(lines) or "(未提取到幻灯片文本)"


def _analyze_xlsx(archive: zipfile.ZipFile, detail: str) -> str:
    shared: list[str] = []
    if "xl/sharedStrings.xml" in archive.namelist():
        try:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root:
                shared.append(
                    "".join(
                        (element.text or "")
                        for element in item.iter()
                        if element.tag.rsplit("}", 1)[-1] == "t"
                    )
                )
        except ET.ParseError:
            pass

    sheet_names: list[str] = []
    if "xl/workbook.xml" in archive.namelist():
        try:
            root = ET.fromstring(archive.read("xl/workbook.xml"))
            sheet_names = [
                element.attrib.get("name", f"Sheet{index}")
                for index, element in enumerate(
                    (
                        item
                        for item in root.iter()
                        if item.tag.rsplit("}", 1)[-1] == "sheet"
                    ),
                    start=1,
                )
            ]
        except ET.ParseError:
            pass

    sheet_files = sorted(
        (
            name
            for name in archive.namelist()
            if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
        ),
        key=_natural_key,
    )
    row_limit = 100 if detail == "full" else 30
    lines: list[str] = []
    for index, name in enumerate(sheet_files, start=1):
        title = sheet_names[index - 1] if index - 1 < len(sheet_names) else f"Sheet{index}"
        try:
            root = ET.fromstring(archive.read(name))
        except (KeyError, ET.ParseError):
            continue
        rows: list[str] = []
        for row in (item for item in root.iter() if item.tag.rsplit("}", 1)[-1] == "row"):
            cells: list[str] = []
            for cell in (item for item in row if item.tag.rsplit("}", 1)[-1] == "c"):
                ref = cell.attrib.get("r", "")
                cell_type = cell.attrib.get("t")
                value_element = next(
                    (
                        item
                        for item in cell
                        if item.tag.rsplit("}", 1)[-1] in {"v", "is"}
                    ),
                    None,
                )
                if value_element is None:
                    value = ""
                elif cell_type == "s":
                    try:
                        value = shared[int(value_element.text or "0")]
                    except (ValueError, IndexError):
                        value = value_element.text or ""
                elif cell_type == "inlineStr":
                    value = "".join(
                        (item.text or "")
                        for item in value_element.iter()
                        if item.tag.rsplit("}", 1)[-1] == "t"
                    )
                else:
                    value = value_element.text or ""
                if value:
                    cells.append(f"{ref}={value}")
            if cells:
                rows.append(" | ".join(cells))
            if len(rows) >= row_limit:
                break
        lines.append(f"[工作表 {title}]")
        lines.extend(rows)
    return "\n".join(lines) or "(未提取到单元格数据)"


def diff_content(
    old_path: str | None = None,
    new_path: str | None = None,
    old_text: str | None = None,
    new_text: str | None = None,
    context_lines: int = 3,
) -> str:
    """生成两个文件或两段文本的 unified diff，供编辑前预览。

    Args:
        old_path: 旧文件路径；与 old_text 二选一。
        new_path: 新文件路径；与 new_text 二选一。
        old_text: 旧文本内容。
        new_text: 新文本内容。
        context_lines: diff 上下文行数，默认 3。
    """
    try:
        context_lines = max(0, min(int(context_lines), 20))
        if old_path and new_path:
            old_target = _safe_path(old_path)
            new_target = _safe_path(new_path)
            if old_target.stat().st_size > MAX_READ_BYTES or new_target.stat().st_size > MAX_READ_BYTES:
                raise ValueError("diff 文件超过单次比较上限")
            old_body = old_target.read_text(encoding="utf-8")
            new_body = new_target.read_text(encoding="utf-8")
            old_label = _relative_label(old_target)
            new_label = _relative_label(new_target)
        elif old_text is not None and new_text is not None:
            old_body, new_body = old_text, new_text
            old_label, new_label = "old", "new"
        else:
            raise ValueError("请提供 old_path/new_path 或 old_text/new_text")

        lines = list(
            difflib.unified_diff(
                old_body.splitlines(),
                new_body.splitlines(),
                fromfile=old_label,
                tofile=new_label,
                lineterm="",
                n=context_lines,
            )
        )
        return _clip("\n".join(lines)) if lines else "无差异。"
    except Exception as exc:
        return f"ERROR: {exc}"


def analyze_file(path: str, detail: str = "summary") -> str:
    """分析工作区内的 Word、PowerPoint、Excel 或常见文本附件。

    Args:
        path: 附件路径，可为工作区内相对路径。
        detail: summary 提取摘要，full 提取更多正文/表格内容。
    """
    try:
        target = _safe_path(path)
        if target.is_dir():
            raise IsADirectoryError(f"这是目录而不是文件：{target}")
        size = target.stat().st_size
        suffix = target.suffix.lower()
        header = f"文件：{_relative_label(target)}\n类型：{suffix or '未知'}\n大小：{size} 字节"
        detail = "full" if str(detail).lower() in {"full", "all"} else "summary"

        if suffix in {".docx", ".pptx", ".xlsx"}:
            with zipfile.ZipFile(target) as archive:
                if suffix == ".docx":
                    body = _analyze_docx(archive, detail)
                elif suffix == ".pptx":
                    body = _analyze_pptx(archive, detail)
                else:
                    body = _analyze_xlsx(archive, detail)
            return _clip(header + "\n\n" + body)

        if suffix in {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".log", ".tsv"}:
            if size > MAX_READ_BYTES:
                raise ValueError("文本附件超过单次分析上限")
            return _clip(header + "\n\n" + target.read_text(encoding="utf-8", errors="replace"))

        if suffix == ".pdf" and shutil.which("pdftotext"):
            proc = subprocess.run(
                ["pdftotext", "-layout", str(target), "-"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
            )
            if proc.returncode == 0:
                return _clip(header + "\n\n" + proc.stdout)

        if suffix in {".doc", ".ppt", ".xls"}:
            return "ERROR：暂不支持旧版二进制 Office 格式，请先另存为 .docx/.pptx/.xlsx。"
        return "ERROR：暂不支持此附件类型；可支持 .docx/.pptx/.xlsx/.csv/.txt/.md/.json/.pdf（若系统有 pdftotext）。"
    except Exception as exc:
        return f"ERROR: {exc}"


def edit_file(
    path: str,
    old_text: str = "",
    new_text: str = "",
    start_line: int | None = None,
    end_line: int | None = None,
    replace_all: bool = False,
) -> Any:
    """精确编辑文本文件；优先按 old_text 替换，也可按行号范围替换。

    Args:
        path: 工作区内文件路径。
        old_text: 要被替换的原文；与行号模式二选一。
        new_text: 替换后的内容。
        start_line: 行模式起始行号，从 1 开始。
        end_line: 行模式结束行号（含），省略时只替换起始行。
        replace_all: old_text 出现多次时是否全部替换。
    """
    try:
        target = _safe_path(path)
        if target.is_dir():
            raise IsADirectoryError(f"这是目录而不是文件：{target}")
        if target.stat().st_size > MAX_READ_BYTES:
            raise ValueError("文件超过单次编辑上限")
        text = target.read_text(encoding="utf-8")

        if old_text:
            count = text.count(old_text)
            if count == 0:
                raise ValueError("old_text 未在文件中找到，请先 read/grep 获取准确原文")
            if count > 1 and not replace_all:
                raise ValueError(f"old_text 出现 {count} 次；请提供更长上下文或设置 replace_all=true")
            updated = text.replace(old_text, new_text, -1 if replace_all else 1)
            changed = count if replace_all else 1
        elif start_line is not None:
            lines = text.splitlines(keepends=True)
            start = int(start_line)
            stop = len(lines) if end_line is None else int(end_line)
            if start < 1 or stop < start or start > len(lines):
                raise ValueError(f"行范围无效：{start_line}-{end_line}")
            replacement = new_text
            if lines and not replacement.endswith("\n") and lines[stop - 1].endswith("\n"):
                replacement += "\n"
            lines[start - 1 : stop] = [replacement]
            updated = "".join(lines)
            changed = stop - start + 1
        else:
            raise ValueError("必须提供 old_text 或 start_line")

        diff = _clip(
            "\n".join(
                difflib.unified_diff(
                    text.splitlines(),
                    updated.splitlines(),
                    fromfile="old",
                    tofile=_relative_label(target),
                    lineterm="",
                    n=3,
                )
            )
        )
        target.write_text(updated, encoding="utf-8", newline="")
        return {
            "ok": True,
            "message": f"成功编辑 {_relative_label(target)}（替换 {changed} 处/行）",
            "path": _relative_label(target),
            "diff": diff,
        }
    except Exception as exc:
        return f"ERROR: {exc}"


def _iter_organize_files(root: Path, recursive: bool) -> list[Path]:
    if recursive:
        files: list[Path] = []
        for current, dirnames, filenames in os.walk(root):
            dirnames[:] = [name for name in dirnames if name not in _EXCLUDED_DIRS and not name.startswith(".")]
            files.extend(Path(current) / name for name in filenames if not name.startswith("."))
        return files
    return [path for path in root.iterdir() if path.is_file() and not path.name.startswith(".")]


def _extension_category(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".doc", ".docx", ".odt", ".rtf", ".txt", ".md"}:
        return "Documents"
    if suffix in {".ppt", ".pptx", ".odp", ".key"}:
        return "Presentations"
    if suffix in {".xls", ".xlsx", ".ods", ".csv", ".tsv"}:
        return "Spreadsheets"
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}:
        return "Images"
    if suffix in {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"}:
        return "Archives"
    if suffix in {".mp3", ".wav", ".flac", ".m4a", ".aac"}:
        return "Audio"
    if suffix in {".mp4", ".mkv", ".mov", ".avi", ".webm"}:
        return "Video"
    if suffix in {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".c", ".cpp", ".h", ".go", ".rs", ".sh", ".html", ".css", ".json", ".yaml", ".yml"}:
        return "Code"
    return "Other"


def organize_files(
    directory: str = ".",
    mode: str = "extension",
    recursive: bool = False,
    dry_run: bool = True,
    confirm: bool = False,
) -> Any:
    """按文件类型或修改日期整理目录，默认只预览不移动。

    Args:
        directory: 要整理的目录。
        mode: extension 按类型分组，date 按年月分组。
        recursive: 是否递归处理子目录文件。
        dry_run: true 只返回计划，false 才实际移动。
        confirm: 实际移动时必须显式设为 true。
    """
    try:
        root = _safe_path(directory)
        if not root.is_dir():
            raise NotADirectoryError(f"不是目录：{root}")
        if mode not in {"extension", "date"}:
            raise ValueError("mode 只能是 extension 或 date")
        if not dry_run and not confirm:
            return "ERROR：实际整理必须先 dry_run=true 预览，再以 dry_run=false、confirm=true 执行。"
        files = _iter_organize_files(root, recursive)
        plans: list[tuple[Path, Path]] = []
        for source in files:
            if mode == "extension":
                category = _extension_category(source)
            else:
                category = datetime.fromtimestamp(source.stat().st_mtime).strftime("%Y-%m")
            destination_dir = root / category
            destination = destination_dir / source.name
            if destination.exists() and destination != source:
                stem, suffix = source.stem, source.suffix
                counter = 1
                while destination.exists():
                    destination = destination_dir / f"{stem} ({counter}){suffix}"
                    counter += 1
            if destination != source:
                plans.append((source, destination))

        preview = [
            {"from": _relative_label(src), "to": _relative_label(dst)}
            for src, dst in plans[:200]
        ]
        if dry_run or not plans:
            return {
                "ok": True,
                "dry_run": True,
                "message": f"计划移动 {len(plans)} 个文件（dry_run）",
                "preview": preview,
            }

        moved = 0
        destinations: list[str] = []
        for source, destination in plans:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
            destinations.append(_relative_label(destination))
            moved += 1
        return {
            "ok": True,
            "message": f"成功整理并移动 {moved} 个文件到 {_relative_label(root)}",
            "path": _relative_label(root),
            "paths": destinations,
        }
    except Exception as exc:
        return f"ERROR: {exc}"


def rename_files(
    directory: str = ".",
    pattern: str = "",
    replacement: str = "",
    glob: str | None = None,
    recursive: bool = False,
    dry_run: bool = True,
    confirm: bool = False,
) -> Any:
    """按正则表达式批量重命名文件，默认只预览不执行。

    Args:
        directory: 目标目录。
        pattern: 匹配原文件名的 Python 正则表达式。
        replacement: 替换文本，可使用 \\1 等正则分组。
        glob: 可选文件名通配符，例如 *.jpg。
        recursive: 是否递归子目录。
        dry_run: true 只返回计划，false 才实际重命名。
        confirm: 实际重命名时必须显式设为 true。
    """
    try:
        root = _safe_path(directory)
        if not root.is_dir():
            raise NotADirectoryError(f"不是目录：{root}")
        if not pattern:
            raise ValueError("pattern 不能为空")
        if not dry_run and not confirm:
            return "ERROR：实际重命名必须先 dry_run=true 预览，再以 dry_run=false、confirm=true 执行。"
        rx = re.compile(pattern)
        files = _iter_organize_files(root, recursive)
        plans: list[tuple[Path, Path]] = []
        for source in files:
            if glob and not fnmatch.fnmatch(source.name, glob):
                continue
            new_name = rx.sub(replacement, source.name)
            if not new_name or new_name in {".", ".."} or Path(new_name).name != new_name:
                raise ValueError(f"重命名结果非法：{new_name!r}")
            destination = source.with_name(new_name)
            if destination == source:
                continue
            if destination.exists():
                raise FileExistsError(f"目标已存在：{destination}")
            plans.append((source, destination))

        preview = [
            {"from": _relative_label(src), "to": _relative_label(dst)}
            for src, dst in plans[:200]
        ]
        if dry_run or not plans:
            return {
                "ok": True,
                "dry_run": True,
                "message": f"计划重命名 {len(plans)} 个文件（dry_run）",
                "preview": preview,
            }

        renamed = 0
        destinations: list[str] = []
        for source, destination in plans:
            source.rename(destination)
            destinations.append(_relative_label(destination))
            renamed += 1
        return {
            "ok": True,
            "message": f"成功重命名 {renamed} 个文件",
            "paths": destinations,
        }
    except Exception as exc:
        return f"ERROR: {exc}"


def list_files(path: str = ".", recursive: bool = False, show_hidden: bool = False) -> Any:
    """列出工作区目录内容，返回适合图形化展示的文件条目。

    Args:
        path: 目录路径。
        recursive: 是否递归列出子目录。
        show_hidden: 是否显示隐藏文件。
    """
    try:
        root = _safe_path(path)
        if not root.is_dir():
            raise NotADirectoryError(f"不是目录：{root}")
        entries: list[dict[str, Any]] = []
        if recursive:
            iterator = (
                item
                for item in sorted(root.rglob("*"), key=lambda value: str(value).lower())
                if item != root
            )
        else:
            iterator = iter(sorted(root.iterdir(), key=lambda value: value.name.lower()))
        for item in iterator:
            if not show_hidden and item.name.startswith("."):
                continue
            try:
                stat = item.stat()
                entries.append(
                    {
                        "name": item.name,
                        "path": _relative_label(item),
                        "type": "dir" if item.is_dir() else "file",
                        "size": 0 if item.is_dir() else stat.st_size,
                        "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                    }
                )
            except OSError:
                continue
            if len(entries) >= 500:
                break
        return {"ok": True, "path": _relative_label(root), "entries": entries}
    except Exception as exc:
        return f"ERROR: {exc}"


def delete_path(
    path: str,
    recursive: bool = False,
    dry_run: bool = True,
    confirm: bool = False,
) -> Any:
    """删除工作区内的文件或目录，默认只预览不删除。

    Args:
        path: 要删除的文件或目录。
        recursive: 目录是否允许递归删除。
        dry_run: true 只返回计划，false 才实际删除。
        confirm: 实际删除时必须显式设为 true。
    """
    try:
        target = _safe_path(path)
        workspace = get_workspace()
        if target == workspace:
            raise ValueError("不能删除整个工作区")
        if not dry_run and not confirm:
            return {
                "ok": False,
                "dry_run": True,
                "message": "实际删除必须先 dry_run=true 预览，再以 dry_run=false、confirm=true 执行",
                "path": _relative_label(target),
            }
        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "message": f"计划删除 {_relative_label(target)}",
                "path": _relative_label(target),
                "type": "dir" if target.is_dir() else "file",
            }
        if target.is_dir():
            if recursive:
                shutil.rmtree(target)
            else:
                target.rmdir()
        else:
            target.unlink()
        return {
            "ok": True,
            "message": f"已删除 {_relative_label(target)}",
            "path": _relative_label(target),
        }
    except Exception as exc:
        return f"ERROR: {exc}"


def web_search(query: str, max_results: int = 5, timeout: int = 10) -> Any:
    """使用 requests 搜索公开网页，返回标题、摘要和链接。

    Args:
        query: 搜索关键词。
        max_results: 返回结果数量，最大 10。
        timeout: HTTP 超时秒数，最大 20。
    """
    try:
        import requests
    except ImportError:
        return {"ok": False, "error": "requests 未安装"}
    query = str(query or "").strip()
    if not query:
        return {"ok": False, "error": "query 不能为空"}
    max_results = max(1, min(int(max_results), 10))
    timeout = max(1, min(int(timeout), 20))
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": "text/html,application/xhtml+xml",
    }
    errors: list[str] = []
    # DuckDuckGo HTML first: clean titles + real URLs (after unwrap).
    engines = [
        (
            "duckduckgo",
            "https://html.duckduckgo.com/html/",
            lambda response: _parse_ddg_results(response.text, max_results),
        ),
        (
            "bing",
            "https://www.bing.com/search",
            lambda response: _parse_bing_results(response.text, max_results),
        ),
        (
            "baidu",
            "https://www.baidu.com/s",
            lambda response: _parse_baidu_results(response.text, max_results),
        ),
    ]
    if re.search(r"天气|weather|气温|温度|降雨|湿度", query, re.I):
        location = re.sub(
            r"(今天|今日|实时|天气|预报|气温|温度|降雨|湿度|weather|today|\d{1,2}月\d{1,2}日|\d{4}-\d{1,2}-\d{1,2})",
            "",
            query,
            flags=re.I,
        )
        location = re.sub(r"\s+", "", location).strip() or "Shenzhen"
        try:
            weather_url = f"https://wttr.in/{quote(location)}?format=j1"
            weather_response = requests.get(weather_url, headers=headers, timeout=timeout)
            weather_response.raise_for_status()
            payload = weather_response.json()
            current = (payload.get("current_condition") or [{}])[0]
            desc = ((current.get("weatherDesc") or [{}])[0]).get("value", "")
            snippet = (
                f"{desc or '天气未知'}，气温 {current.get('temp_C', '?')}°C，"
                f"体感 {current.get('FeelsLikeC', '?')}°C，湿度 {current.get('humidity', '?')}%，"
                f"风速 {current.get('windspeedKmph', '?')} km/h"
            )
            area = ((payload.get("nearest_area") or [{}])[0]).get("areaName", [{}])
            area_name = (area or [{}])[0].get("value", location)
            return {
                "ok": True,
                "query": query,
                "engine": "wttr.in",
                "results": [
                    {
                        "title": f"{area_name}实时天气",
                        "url": f"https://wttr.in/{quote(location)}",
                        "snippet": snippet,
                    }
                ],
            }
        except Exception as exc:
            errors.append(f"wttr.in: {exc}")

    for name, url, parser in engines:
        try:
            response = requests.get(
                url,
                params={"q": query} if name in {"bing", "duckduckgo"} else {"wd": query},
                headers=headers,
                timeout=timeout,
            )
            response.raise_for_status()
            results = parser(response)
            if results:
                return {"ok": True, "query": query, "engine": name, "results": results}
            errors.append(f"{name}: 没有解析到结果")
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return {"ok": False, "query": query, "error": "；".join(errors)}


def _clean_html(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    return html_lib.unescape(value).replace("\\xa0", " ").strip()


def _unwrap_search_url(url: str) -> str:
    """Resolve Bing / DuckDuckGo click-through redirects to the real target URL."""
    url = html_lib.unescape(str(url or "")).strip()
    if not url:
        return url
    if url.startswith("//"):
        url = "https:" + url
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    query = parse_qs(parsed.query)
    host = (parsed.hostname or "").lower()
    # DuckDuckGo: //duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com
    if "duckduckgo.com" in host:
        real = (query.get("uddg") or [""])[0]
        if real:
            return unquote(real)
    # Bing: https://www.bing.com/ck/a?...&u=a1<base64url>
    if "bing.com" in host and parsed.path.startswith("/ck/"):
        u = (query.get("u") or [""])[0]
        if u.startswith("a1"):
            raw = u[2:]
            pad = "=" * ((-len(raw)) % 4)
            try:
                decoded = base64.urlsafe_b64decode(raw + pad).decode("utf-8", "replace")
                if decoded.startswith("http"):
                    return decoded
            except (ValueError, TypeError):
                pass
    return url


def _parse_ddg_results(text: str, max_results: int) -> list[dict[str, str]]:
    pattern = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'(?:class="result__snippet"[^>]*>(.*?)</a>)?',
        re.I | re.S,
    )
    results: list[dict[str, str]] = []
    for match in pattern.finditer(text):
        if len(results) >= max_results:
            break
        href, title, snippet = match.groups()
        url = _unwrap_search_url(href)
        if not url.startswith("http"):
            continue
        title_text = _clean_html(title)
        if not title_text:
            continue
        results.append(
            {
                "title": title_text,
                "url": url,
                "snippet": _clean_html(snippet),
            }
        )
    return results


def _parse_bing_results(text: str, max_results: int) -> list[dict[str, str]]:
    blocks = re.split(r'<li class="b_algo"', text)[1:]
    results: list[dict[str, str]] = []
    for block in blocks:
        if len(results) >= max_results:
            break
        link = re.search(
            r'<h2[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            block,
            re.I | re.S,
        )
        if not link:
            continue
        href, title = link.groups()
        url = _unwrap_search_url(href)
        if not url.startswith("http"):
            continue
        snippet_match = re.search(
            r'<p[^>]*>(.*?)</p>',
            block,
            re.I | re.S,
        )
        title_text = _clean_html(title)
        if not title_text:
            continue
        results.append(
            {
                "title": title_text,
                "url": url,
                "snippet": _clean_html(snippet_match.group(1)) if snippet_match else "",
            }
        )
    return results


def _parse_baidu_results(text: str, max_results: int) -> list[dict[str, str]]:
    blocks = re.findall(r'<div[^>]+class="[^"]*result[^"]*"[^>]*>.*?</div>\s*</div>', text, re.I | re.S)
    results: list[dict[str, str]] = []
    for block in blocks:
        if len(results) >= max_results:
            break
        link = re.search(r'<h3[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.I | re.S)
        if not link:
            continue
        href, title = link.groups()
        snippet_match = re.search(r'class="[^"]*(?:content-right|c-abstract)[^"]*"[^>]*>(.*?)</', block, re.I | re.S)
        results.append(
            {
                "title": _clean_html(title),
                "url": html_lib.unescape(href).strip(),
                "snippet": _clean_html(snippet_match.group(1)) if snippet_match else "",
            }
        )
    if not results:
        links = re.findall(r'<h3[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', text, re.I | re.S)
        for href, title in links[:max_results]:
            results.append({"title": _clean_html(title), "url": html_lib.unescape(href).strip(), "snippet": ""})
    return results



def update_plan(
    title: str,
    steps: list[str],
    plan: list[dict[str, Any]],
    completed_steps: list[int] | None = None,
) -> str:
    """设置或更新当前会话的执行计划。

    Args:
        title: 计划标题。
        steps: 有序步骤列表。
        completed_steps: 已完成步骤的 1 起始编号列表，可省略。
    """
    if not title.strip():
        return "ERROR: title 不能为空"
    if not steps:
        return "ERROR: steps 不能为空"
    if len(steps) > 20:
        return "ERROR: 步骤不能超过 20 个"
    completed = {int(value) for value in (completed_steps or [])}
    plan.clear()
    plan.append({"title": title.strip(), "steps": []})
    for index, step in enumerate(steps, start=1):
        plan[0]["steps"].append(
            {"index": index, "text": str(step).strip(), "done": index in completed}
        )
    lines = [f"# {title.strip()}"]
    for item in plan[0]["steps"]:
        mark = "x" if item["done"] else " "
        lines.append(f"- [{mark}] {item['index']}. {item['text']}")
    return "\n".join(lines)


IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".avif"}


def show_content(
    kind: str,
    target: str,
    title: str = "",
    caption: str = "",
) -> Any:
    """向用户展示内容：图片、网页、表格、思维导图、文件或文本。

    Args:
        kind: image | url | file | text | table | mindmap | mermaid
        target: 路径 / URL / JSON（table、mindmap）/ 正文
        title: 可选标题
        caption: 可选说明文字
    """
    try:
        kind = str(kind or "text").strip().lower()
        target = str(target or "").strip()
        if not target:
            raise ValueError("target 不能为空")
        title = str(title or "").strip()[:120]
        caption = str(caption or "").strip()[:500]

        if kind == "image":
            if re.match(r"^https?://", target, re.I):
                return {
                    "ok": True,
                    "kind": "image",
                    "src": target,
                    "title": title or "图片",
                    "caption": caption,
                    "external": True,
                }
            path = _safe_path(target)
            if path.suffix.lower() not in IMAGE_EXTS:
                raise ValueError(f"不是支持的图片格式：{path.suffix or '(无扩展名)'}")
            rel = _relative_label(path)
            return {
                "ok": True,
                "kind": "image",
                "src": "/api/file?path=" + quote(rel, safe=""),
                "title": title or path.name,
                "caption": caption,
                "path": rel,
                "external": False,
            }

        if kind == "url":
            if not re.match(r"^https?://", target, re.I):
                raise ValueError("url 需要以 http:// 或 https:// 开头")
            host = urlparse(target).netloc
            return {
                "ok": True,
                "kind": "url",
                "url": target,
                "host": host,
                "title": title or host or target,
                "caption": caption,
                "message": f"网页 · {host}",
            }

        if kind == "file":
            path = _safe_path(target)
            rel = _relative_label(path)
            return {
                "ok": True,
                "kind": "file",
                "path": rel,
                "title": title or path.name,
                "caption": caption,
                "message": f"已准备展示文件 {rel}",
            }

        if kind in {"text", "markdown", "md"}:
            body = target[:8000]
            return {
                "ok": True,
                "kind": "text",
                "text": body,
                "title": title or "展示",
                "caption": caption,
                "message": caption or title or "已准备展示文本",
            }

        if kind == "table":
            rows = _parse_table_target(target)
            if not rows:
                raise ValueError("table 需要非空 JSON 数组（每项为一行对象）")
            headers: list[str] = []
            for row in rows:
                for key in row.keys():
                    if key not in headers:
                        headers.append(str(key))
            return {
                "ok": True,
                "kind": "table",
                "headers": headers[:40],
                "rows": [list(r.values()) for r in rows[:200]],
                "row_dicts": rows[:200],
                "title": title or "表格",
                "caption": caption,
                "message": caption or f"表格 · {len(rows)} 行 × {len(headers)} 列",
                "path": None,
            }

        if kind == "mindmap":
            tree = _parse_mindmap_target(target)
            return {
                "ok": True,
                "kind": "mindmap",
                "tree": tree,
                "title": title or str(tree.get("label") or "思维导图"),
                "caption": caption,
                "message": caption or "思维导图已生成",
            }

        if kind == "mermaid":
            return {
                "ok": True,
                "kind": "mermaid",
                "code": target[:12000],
                "title": title or "图表",
                "caption": caption,
                "message": caption or "Mermaid 图已准备",
            }

        raise ValueError("kind 只能是 image / url / file / text / table / mindmap / mermaid")
    except Exception as exc:
        return {"ok": False, "error": f"ERROR: {exc}"}


def _parse_table_target(target: str) -> list[dict[str, Any]]:
    import json as _json

    raw = target.strip()
    if not raw:
        return []
    data = _json.loads(raw)
    if isinstance(data, dict):
        # {headers:[], rows:[[]]} or {columns:[], data:[]}
        headers = data.get("headers") or data.get("columns") or []
        rows = data.get("rows") or data.get("data") or []
        out: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, dict):
                out.append(row)
            elif isinstance(row, (list, tuple)):
                out.append({str(headers[i] if i < len(headers) else f"c{i}"): row[i] for i in range(len(row))})
        return out
    if isinstance(data, list):
        out = []
        for item in data:
            if isinstance(item, dict):
                out.append(item)
            elif isinstance(item, (list, tuple)):
                out.append({f"c{i}": v for i, v in enumerate(item)})
        return out
    raise ValueError("table JSON 必须是数组或 {headers,rows} 对象")


def _parse_mindmap_target(target: str) -> dict[str, Any]:
    import json as _json

    raw = target.strip()
    data = _json.loads(raw)

    def norm_node(node: Any) -> dict[str, Any]:
        if isinstance(node, dict):
            label = node.get("label") or node.get("name") or node.get("title") or "节点"
            kids = node.get("children") or node.get("items") or []
            if not isinstance(kids, list):
                kids = [kids]
            return {"label": str(label), "children": [norm_node(k) for k in kids]}
        return {"label": str(node), "children": []}

    if isinstance(data, str):
        return {"label": data, "children": []}
    if isinstance(data, dict):
        if "label" in data or "name" in data or "title" in data or "children" in data:
            return norm_node(data)
        # single key map: {root: children}
        if len(data) == 1:
            k, v = next(iter(data.items()))
            node = norm_node({"label": k, "children": v if isinstance(v, list) else [v]})
            return node
        return norm_node({"label": "中心", "children": list(data.items())})
    if isinstance(data, list):
        return {"label": "中心", "children": [norm_node(x) for x in data]}
    raise ValueError("mindmap 需要 JSON 对象 {label, children} 或字符串数组")


def office_tool(
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
    """Office 文档/表格结构化读写（xlsx / csv / docx）。

    Args:
        path: 工作区内文件路径；create 时为目标路径。
        action: inspect | read | write | append_rows | create | create_doc | replace_text。
        sheet: 工作表名（xlsx）。
        range_ref: 单元格区域，如 A1:D20。
        cells: 写入映射，如 {"A1": "值", "B2": 1}。
        rows: append_rows 的行数据（对象数组）。
        text: create_doc / replace_text 的正文或替换文本。
        create: 文件类型 xlsx|csv|docx（配合 action=create）。
        confirm: 实际写入/创建时必须为 true。
    """
    try:
        action = str(action or "inspect").strip().lower()
        suffix_create = str(create or "").strip().lower()
        if action == "create":
            if not confirm:
                return {"ok": False, "error": "ERROR：创建文件需要 confirm=true"}
            if suffix_create not in {"xlsx", "csv", "docx"}:
                raise ValueError("create 需为 xlsx / csv / docx")
            target = _safe_path(path, allow_missing=True)
            if target.exists():
                raise FileExistsError(f"文件已存在：{_relative_label(target)}")
            if suffix_create == "xlsx":
                from openpyxl import Workbook

                wb = Workbook()
                ws = wb.active
                ws.title = sheet or "Sheet1"
                if rows:
                    headers = list(rows[0].keys())
                    ws.append(headers)
                    for row in rows:
                        ws.append([row.get(h) for h in headers])
                target.parent.mkdir(parents=True, exist_ok=True)
                wb.save(target)
            elif suffix_create == "csv":
                import csv

                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("w", encoding="utf-8-sig", newline="") as fh:
                    writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()) if rows else [])
                    if rows:
                        writer.writeheader()
                        writer.writerows(rows)
            else:
                from docx import Document

                doc = Document()
                for line in str(text or "").splitlines():
                    doc.add_paragraph(line)
                if rows:
                    table = doc.add_table(rows=1, cols=len(rows[0]))
                    headers = list(rows[0].keys())
                    for i, h in enumerate(headers):
                        table.rows[0].cells[i].text = str(h)
                    for row in rows:
                        cells_row = table.add_row().cells
                        for i, h in enumerate(headers):
                            cells_row[i].text = str(row.get(h, ""))
                target.parent.mkdir(parents=True, exist_ok=True)
                doc.save(target)
            return {
                "ok": True,
                "action": "create",
                "path": _relative_label(target),
                "message": f"已创建 {target.name}",
                "paths": [_relative_label(target)],
            }

        target = _safe_path(path)
        suffix = target.suffix.lower()

        if action == "inspect":
            if suffix in {".xlsx", ".xlsm"}:
                import openpyxl

                wb = openpyxl.load_workbook(target, read_only=True, data_only=True)
                info = {
                    "ok": True,
                    "action": "inspect",
                    "kind": "spreadsheet",
                    "path": _relative_label(target),
                    "sheets": wb.sheetnames,
                    "message": f"工作簿 · {len(wb.sheetnames)} 个工作表",
                }
                preview = {}
                for name in wb.sheetnames[:4]:
                    ws = wb[name]
                    rows_preview = []
                    for i, row in enumerate(ws.iter_rows(values_only=True)):
                        rows_preview.append([("" if v is None else v) for v in row[:12]])
                        if i >= 7:
                            break
                    preview[name] = rows_preview
                wb.close()
                info["preview"] = preview
                return info
            if suffix == ".csv":
                import csv

                with target.open(encoding="utf-8-sig", newline="") as fh:
                    reader = csv.reader(fh)
                    rows_preview = [next(reader) for _ in range(9)]
                return {
                    "ok": True,
                    "action": "inspect",
                    "kind": "csv",
                    "path": _relative_label(target),
                    "preview": {"csv": rows_preview},
                    "message": "CSV 表格",
                }
            if suffix == ".docx":
                from docx import Document

                doc = Document(str(target))
                paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
                return {
                    "ok": True,
                    "action": "inspect",
                    "kind": "document",
                    "path": _relative_label(target),
                    "paragraphs": paras[:40],
                    "tables": len(doc.tables),
                    "message": f"文档 · {len(paras)} 段 · {len(doc.tables)} 表",
                    "text": "\n".join(paras[:80]),
                }
            raise ValueError(f"office 不支持的类型：{suffix or path}")

        if action == "read":
            if suffix in {".xlsx", ".xlsm"}:
                import openpyxl

                wb = openpyxl.load_workbook(target, read_only=True, data_only=True)
                ws_name = sheet or wb.sheetnames[0]
                if ws_name not in wb.sheetnames:
                    raise ValueError(f"没有工作表：{ws_name}")
                ws = wb[ws_name]
                matrix = []
                for row in ws.iter_rows(values_only=True):
                    matrix.append(["" if v is None else v for v in row])
                    if len(matrix) >= 200:
                        break
                wb.close()
                headers = [str(h) if h is not None else f"c{i}" for i, h in enumerate(matrix[0] if matrix else [])]
                dicts = []
                for row in matrix[1:]:
                    dicts.append({headers[i] if i < len(headers) else f"c{i}": (row[i] if i < len(row) else "") for i in range(max(len(headers), len(row)))})
                return {
                    "ok": True,
                    "action": "read",
                    "kind": "table",
                    "path": _relative_label(target),
                    "sheet": ws_name,
                    "headers": headers,
                    "rows": matrix[1:80],
                    "row_dicts": dicts[:80],
                    "title": f"{target.name} · {ws_name}",
                    "message": f"读取 {ws_name}：{len(dicts)} 行",
                    "kind_show": "table",
                }
            if suffix == ".csv":
                import csv

                with target.open(encoding="utf-8-sig", newline="") as fh:
                    matrix = list(csv.reader(fh))[:200]
                headers = matrix[0] if matrix else []
                dicts = [
                    {headers[i] if i < len(headers) else f"c{i}": (row[i] if i < len(row) else "") for i in range(max(len(headers), len(row)))}
                    for row in matrix[1:]
                ]
                return {
                    "ok": True,
                    "action": "read",
                    "kind": "table",
                    "path": _relative_label(target),
                    "headers": headers,
                    "rows": matrix[1:80],
                    "row_dicts": dicts[:80],
                    "title": target.name,
                    "message": f"读取 CSV：{len(dicts)} 行",
                    "kind_show": "table",
                }
            if suffix == ".docx":
                from docx import Document

                doc = Document(str(target))
                paras = [p.text for p in doc.paragraphs]
                tables_out = []
                for ti, table in enumerate(doc.tables[:6]):
                    trows = [[cell.text for cell in row.cells] for row in table.rows[:50]]
                    tables_out.append(trows)
                return {
                    "ok": True,
                    "action": "read",
                    "kind": "document",
                    "path": _relative_label(target),
                    "text": "\n".join(paras)[:12000],
                    "tables": tables_out,
                    "title": target.name,
                    "message": f"读取文档 · {len(paras)} 段 · {len(doc.tables)} 表",
                }
            raise ValueError(f"不支持读取：{suffix}")

        if action == "write":
            if not confirm:
                return {"ok": False, "error": "ERROR：写入需要 confirm=true"}
            if not cells:
                raise ValueError("write 需要 cells 映射")
            if suffix in {".xlsx", ".xlsm"}:
                import openpyxl

                wb = openpyxl.load_workbook(target)
                ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb.active
                for ref, value in cells.items():
                    ws[ref] = value
                wb.save(target)
                return {
                    "ok": True,
                    "action": "write",
                    "path": _relative_label(target),
                    "message": f"已写入 {len(cells)} 个单元格到 {ws.title}",
                    "paths": [_relative_label(target)],
                }
            raise ValueError(f"write 仅支持 xlsx，当前 {suffix}")

        if action == "append_rows":
            if not confirm:
                return {"ok": False, "error": "ERROR：append_rows 需要 confirm=true"}
            if not rows:
                raise ValueError("rows 不能为空")
            if suffix == ".csv":
                import csv

                with target.open(encoding="utf-8-sig", newline="") as fh:
                    existing = list(csv.reader(fh))
                headers = existing[0] if existing else list(rows[0].keys())
                with target.open("w", encoding="utf-8-sig", newline="") as fh:
                    writer = csv.DictWriter(fh, fieldnames=headers)
                    if not existing:
                        writer.writeheader()
                    writer.writerows(rows)
                return {
                    "ok": True,
                    "action": "append_rows",
                    "path": _relative_label(target),
                    "message": f"已追加 {len(rows)} 行",
                    "paths": [_relative_label(target)],
                }
            if suffix in {".xlsx", ".xlsm"}:
                import openpyxl

                wb = openpyxl.load_workbook(target)
                ws = wb[sheet] if sheet and sheet in wb.sheetnames else wb.active
                headers = list(rows[0].keys())
                # append after last row
                start = ws.max_row + 1 if ws.max_row else 1
                if start == 1:
                    ws.append(headers)
                    start = 2
                for row in rows:
                    ws.append([row.get(h) for h in headers])
                wb.save(target)
                return {
                    "ok": True,
                    "action": "append_rows",
                    "path": _relative_label(target),
                    "message": f"已追加 {len(rows)} 行到 {ws.title}",
                    "paths": [_relative_label(target)],
                }
            raise ValueError(f"append_rows 不支持 {suffix}")

        if action == "replace_text":
            if not confirm:
                return {"ok": False, "error": "ERROR：replace_text 需要 confirm=true"}
            if suffix != ".docx" or text is None:
                raise ValueError("replace_text 仅支持 docx 且需要 text")
            # text format: old=>new per line
            from docx import Document

            doc = Document(str(target))
            replaced = 0
            for line in str(text).splitlines():
                if "=>" not in line:
                    continue
                old, new = line.split("=>", 1)
                old, new = old.strip(), new.strip()
                if not old:
                    continue
                for p in doc.paragraphs:
                    if old in p.text:
                        # simple full-paragraph rewrite when possible
                        if p.text.strip() == old:
                            p.text = new
                            replaced += 1
                        else:
                            p.text = p.text.replace(old, new)
                            replaced += 1
            doc.save(target)
            return {
                "ok": True,
                "action": "replace_text",
                "path": _relative_label(target),
                "message": f"已替换 {replaced} 处",
                "paths": [_relative_label(target)],
            }

        if action == "create_doc":
            if not confirm:
                return {"ok": False, "error": "ERROR：create_doc 需要 confirm=true"}
            from docx import Document

            doc = Document()
            for line in str(text or "").splitlines():
                doc.add_paragraph(line)
            target.parent.mkdir(parents=True, exist_ok=True)
            doc.save(target)
            return {
                "ok": True,
                "action": "create_doc",
                "path": _relative_label(target),
                "message": f"已创建文档 {target.name}",
                "paths": [_relative_label(target)],
            }

        raise ValueError(f"未知 action：{action}")
    except Exception as exc:
        return {"ok": False, "error": f"ERROR: {exc}"}
