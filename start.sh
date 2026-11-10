#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

MODEL_PATH="${SPARK_MODEL:-}"
MODEL_DIR="${SPARK_MODEL_DIR:-$HOME/models}"
LLAMA_BIN="${SPARK_LLAMA_BIN:-}"
LLAMA_PORT="${SPARK_LLAMA_PORT:-8080}"
WEB_PORT="${SPARK_WEB_PORT:-8787}"
WEB_HOST="${SPARK_WEB_HOST:-127.0.0.1}"
CONTEXT_SIZE="${SPARK_CONTEXT_SIZE:-65536}"
GPU_LAYERS="${SPARK_GPU_LAYERS:-18}"
FLASH_ATTN="${SPARK_FLASH_ATTN:-on}"
CACHE_K="${SPARK_CACHE_K:-q4_0}"
CACHE_V="${SPARK_CACHE_V:-q4_0}"
THREADS="${SPARK_THREADS:-8}"
THREADS_BATCH="${SPARK_THREADS_BATCH:-8}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${SPARK_VENV:-$ROOT_DIR/.venv}"
RUN_DIR="$ROOT_DIR/.run"
LLAMA_LOG="$RUN_DIR/llama.log"
LLAMA_PID_FILE="$RUN_DIR/llama.pid"
START_LLAMA=1
RESET_SESSION=0
INSTALL_ONLY=0

usage() {
  cat <<'USAGE'
Spark Agent 一键启动（可移植版）

用法:
  ./start.sh [选项]

选项:
  --ui-only       不启动 llama.cpp（用于连接已有 SPARK_LLAMA_BASE_URL）
  --reset         启动前清空所有 Web 会话
  --install-only  只创建虚拟环境并安装依赖
  -h, --help      显示帮助

常用环境变量:
  SPARK_LLAMA_BIN=/usr/bin/llama-server     # llama-server 路径（也可在 Web 设置里填）
  SPARK_MODEL_DIR=$HOME/models              # GGUF 扫描目录（也可在 Web 设置里填）
  SPARK_MODEL=/path/to/model.gguf           # 默认模型（可选）
  SPARK_LLAMA_PORT=8080
  SPARK_WEB_PORT=8787
  SPARK_WORKSPACE=/path/to/project
  SPARK_CONTEXT_SIZE=65536
  SPARK_GPU_LAYERS=18
  SPARK_SHELL_SANDBOX=auto
  SPARK_SHELL_NETWORK=1
USAGE
}

while (($#)); do
  case "$1" in
    --ui-only) START_LLAMA=0 ;;
    --reset) RESET_SESSION=1 ;;
    --install-only) INSTALL_ONLY=1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf '未知参数: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

mkdir -p "$RUN_DIR"

if [[ -f "$RUN_DIR/settings.json" ]]; then
  SETTINGS_LINE="$(python3 - "$RUN_DIR/settings.json" <<'PY_SETTINGS'
import json
import sys
from pathlib import Path
defaults = {
    "context_size": 65536,
    "gpu_layers": "18",
    "kv_cache": "q4_0",
    "threads": 8,
    "threads_batch": 8,
    "flash_attention": "on",
    "model": "",
    "llama_bin": "",
    "model_dir": "",
}
try:
    raw = json.loads(Path(sys.argv[1]).read_text())
except Exception:
    raw = {}
values = []
for key in (
    "context_size", "gpu_layers", "kv_cache", "threads", "threads_batch",
    "flash_attention", "model", "llama_bin", "model_dir",
):
    value = raw.get(key, defaults.get(key, ""))
    values.append(str(value).replace("\\t", " ").replace("\\n", " "))
print("\t".join(values))
PY_SETTINGS
)"
  IFS=$'\t' read -r CONTEXT_SIZE GPU_LAYERS CACHE_K THREADS THREADS_BATCH FLASH_ATTN MODEL_FILE LLAMA_BIN_SETTING MODEL_DIR_SETTING <<< "$SETTINGS_LINE"
  [[ -n "${LLAMA_BIN_SETTING:-}" ]] && LLAMA_BIN="$LLAMA_BIN_SETTING"
  [[ -n "${MODEL_DIR_SETTING:-}" ]] && MODEL_DIR="$MODEL_DIR_SETTING"
  [[ -n "${MODEL_FILE:-}" && -z "$MODEL_PATH" ]] && MODEL_PATH="$MODEL_FILE"
fi

# Resolve llama-server: explicit → settings/env → PATH → common paths
if [[ -z "$LLAMA_BIN" ]]; then
  LLAMA_BIN="$(command -v llama-server 2>/dev/null || true)"
fi
if [[ -z "$LLAMA_BIN" ]]; then
  for cand in /usr/bin/llama-server /usr/local/bin/llama-server \
    "$HOME/llama.cpp/build/bin/llama-server" "$HOME/.local/bin/llama-server"; do
    if [[ -x "$cand" ]]; then LLAMA_BIN="$cand"; break; fi
  done
fi

# Resolve model path from model_dir if needed
mkdir -p "$MODEL_DIR" 2>/dev/null || true
if [[ -z "$MODEL_PATH" ]]; then
  MODEL_PATH="$(find "$MODEL_DIR" -maxdepth 1 -type f -name '*.gguf' ! -name '*mmproj*' 2>/dev/null | head -n1 || true)"
fi

export SPARK_LLAMA_BIN="${LLAMA_BIN:-}"
export SPARK_MODEL_DIR="$MODEL_DIR"
if [[ -n "$MODEL_PATH" ]]; then
  export SPARK_MODEL="$MODEL_PATH"
fi

printf '==> 检查运行环境\n'
command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
  printf '未找到 Python：%s\n' "$PYTHON_BIN" >&2
  exit 1
}

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  printf '==> 创建虚拟环境 %s\n' "$VENV_DIR"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

if ! "$VENV_DIR/bin/python" -c 'import pydantic_ai, fastapi, uvicorn' >/dev/null 2>&1; then
  printf '==> 安装 Python 依赖（首次运行）\n'
  "$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null
  "$VENV_DIR/bin/python" -m pip install -r "$ROOT_DIR/requirements.txt"
fi

if ((INSTALL_ONLY)); then
  printf '依赖安装完成。\n'
  exit 0
fi

if ((RESET_SESSION)); then
  rm -f "$RUN_DIR/sessions.json"
  printf '==> 已清空会话数据\n'
fi

health_url="http://127.0.0.1:${LLAMA_PORT}/health"
llama_started=0
llama_pid=""

server_healthy() {
  curl -fsS --max-time 2 "$health_url" >/dev/null 2>&1
}

if ((START_LLAMA)); then
  if [[ -z "$LLAMA_BIN" || ! -x "$LLAMA_BIN" ]]; then
    printf '未找到 llama-server。请安装 llama.cpp，或设置 SPARK_LLAMA_BIN，或在 Web 设置里填写路径。\n' >&2
    printf '也可先用 ./start.sh --ui-only 连接已运行的实例。\n' >&2
    exit 1
  fi
  if [[ -z "$MODEL_PATH" || ! -f "$MODEL_PATH" ]]; then
    printf '未找到 GGUF 模型。请将 .gguf 放入模型目录：%s\n' "$MODEL_DIR" >&2
    printf '或设置 SPARK_MODEL=/path/to/model.gguf，或在 Web 设置里选择模型。\n' >&2
    printf '也可先用 ./start.sh --ui-only 连接已运行的实例。\n' >&2
    exit 1
  fi

  printf '==> llama-server: %s\n' "$LLAMA_BIN"
  printf '==> 模型目录: %s\n' "$MODEL_DIR"
  printf '==> 模型文件: %s\n' "$MODEL_PATH"

  if server_healthy; then
    printf '==> llama.cpp 已在 %s 就绪，复用现有实例\n' "$health_url"
  else
    if [[ -f "$LLAMA_PID_FILE" ]] && kill -0 "$(cat "$LLAMA_PID_FILE")" 2>/dev/null; then
      printf '==> 等待已有 llama.cpp 进程…\n'
      for _ in $(seq 1 90); do
        server_healthy && break
        sleep 1
      done
    fi

    if ! server_healthy; then
      printf '==> 启动 llama.cpp（模型：%s）\n' "$MODEL_PATH"
      : > "$LLAMA_LOG"
      if [[ -n "${MODEL_FILE:-}" && -f "$MODEL_FILE" ]]; then MODEL_PATH="$MODEL_FILE"; fi
      env \
        SPARK_MODEL="$MODEL_PATH" \
        SPARK_MODEL_DIR="$MODEL_DIR" \
        SPARK_LLAMA_BIN="$LLAMA_BIN" \
        SPARK_LLAMA_PORT="$LLAMA_PORT" \
        SPARK_CONTEXT_SIZE="$CONTEXT_SIZE" \
        SPARK_GPU_LAYERS="$GPU_LAYERS" \
        "$LLAMA_BIN" \
          --model "$MODEL_PATH" \
          --host 127.0.0.1 \
          --port "$LLAMA_PORT" \
          --ctx-size "$CONTEXT_SIZE" \
          --parallel 1 \
          --n-gpu-layers "$GPU_LAYERS" \
          --flash-attn "$FLASH_ATTN" \
          --cache-type-k "$CACHE_K" \
          --cache-type-v "$CACHE_V" \
          --threads "$THREADS" \
          --threads-batch "$THREADS_BATCH" \
          --cont-batching \
          --alias Spark \
          >>"$LLAMA_LOG" 2>&1 &
      llama_pid=$!
      llama_started=1
      printf '%s\n' "$llama_pid" > "$LLAMA_PID_FILE"

      printf '    等待模型加载'
      for _ in $(seq 1 120); do
        if server_healthy; then
          printf ' 就绪\n'
          break
        fi
        if ! kill -0 "$llama_pid" 2>/dev/null; then
          printf '\nllama.cpp 启动失败，最近日志：\n' >&2
          tail -60 "$LLAMA_LOG" >&2 || true
          rm -f "$LLAMA_PID_FILE"
          exit 1
        fi
        printf '.'
        sleep 1
      done

      server_healthy || {
        printf '\n等待模型超时，日志：%s\n' "$LLAMA_LOG" >&2
        if [[ -n "$llama_pid" ]] && kill -0 "$llama_pid" 2>/dev/null; then
          kill "$llama_pid" 2>/dev/null || true
          wait "$llama_pid" 2>/dev/null || true
        fi
        rm -f "$LLAMA_PID_FILE"
        exit 1
      }
    fi
  fi
fi

cleanup() {
  local status=$?
  local current_pid="$llama_pid"
  if [[ -f "$LLAMA_PID_FILE" ]]; then
    current_pid="$(cat "$LLAMA_PID_FILE" 2>/dev/null || true)"
  fi
  if ((llama_started)) && [[ -n "$current_pid" ]] && kill -0 "$current_pid" 2>/dev/null; then
    printf '\n==> 停止 llama.cpp (pid %s)\n' "$current_pid"
    kill "$current_pid" 2>/dev/null || true
    wait "$current_pid" 2>/dev/null || true
    rm -f "$LLAMA_PID_FILE"
  fi
  exit "$status"
}
trap cleanup INT TERM EXIT

export PYDANTIC_AI_NO_BANNER=1
# 保留用户显式传入的 SPARK_WORKSPACE；否则读取上次通过 Web UI 保存的工作区。
if [[ -n "${SPARK_WORKSPACE:-}" ]]; then
  export SPARK_WORKSPACE
else
  unset SPARK_WORKSPACE
fi
export SPARK_LLAMA_BASE_URL="${SPARK_LLAMA_BASE_URL:-http://127.0.0.1:${LLAMA_PORT}/v1}"
export SPARK_WEB_HOST="$WEB_HOST"
export SPARK_WEB_PORT="$WEB_PORT"

WORKSPACE_DISPLAY="$("$VENV_DIR/bin/python" -c 'from app.config import get_workspace; print(get_workspace())')"
printf '==> 启动 Web UI：http://%s:%s\n' "$WEB_HOST" "$WEB_PORT"
printf '    工作区：%s\n' "$WORKSPACE_DISPLAY"
printf '    按 Ctrl+C 停止\n\n'

"$VENV_DIR/bin/python" -m uvicorn app.main:app \
  --host "$WEB_HOST" \
  --port "$WEB_PORT" \
  --log-level info
