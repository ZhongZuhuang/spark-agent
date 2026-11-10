# Spark Agent（可移植版）

本机优先的 **编码 Agent** Web 应用：llama.cpp（OpenAI 兼容）+ Pydantic AI 工具循环 + 简洁聊天界面。

与开发机专用版本的区别：

- **可脱离原机器部署**：llama-server 路径可自动探测，也可手动填写
- **模型目录可自定义**：在 Web 设置里指定 GGUF 扫描目录
- **已剥离** OCR 工作台与 Computer-Use 工作台

## 环境要求

- Linux / macOS（需 `bash`、`python3` ≥ 3.10）
- [llama.cpp](https://github.com/ggml-org/llama.cpp) 的 `llama-server`（自行编译或包管理器安装）
- 至少一个聊天用 **GGUF** 模型（可选 mmproj 做视觉）
- 可选：`bwrap`（shell 沙箱）

## 快速开始

```bash
git clone <your-repo-url>
cd spark-agent   # 或你的目录名
./start.sh
```

首次会创建 `.venv` 并安装依赖。默认：

- Web：<http://127.0.0.1:8787>
- llama.cpp：<http://127.0.0.1:8080>（若 `--ui-only` 则只起 Web）

只起界面、连已有模型服务：

```bash
SPARK_LLAMA_BASE_URL=http://127.0.0.1:8080/v1 ./start.sh --ui-only
```

## 配置 llama 与模型目录

### 方式一：环境变量

```bash
export SPARK_LLAMA_BIN=/usr/bin/llama-server   # 或你的编译路径
export SPARK_MODEL_DIR=$HOME/models            # 放 .gguf 的目录
export SPARK_MODEL=$HOME/models/my-model.gguf  # 可选默认模型
./start.sh
```

### 方式二：Web 设置（推荐）

打开 **设置 → 运行环境**：

| 字段 | 说明 |
| --- | --- |
| **llama-server 可执行文件** | 留空则自动探测 `PATH` 与常见路径；否则填完整路径 |
| **模型扫描目录** | 列出其中的 `.gguf` 供选择；修改后保存会重启模型 |

保存写入 `.run/settings.json`，并按新路径重启 llama.cpp。

### 自动探测顺序

1. 设置里的 `llama_bin` / 环境变量 `SPARK_LLAMA_BIN`
2. `PATH` 中的 `llama-server`
3. `/usr/bin/llama-server`、`/usr/local/bin/llama-server`
4. `~/llama.cpp/build/bin/llama-server`、`~/.local/bin/llama-server`

## 工作区与安全

- 默认工作区为项目目录；侧栏可切换绝对路径（须已存在）。
- `read` / `write` / `edit` / `shell` 等限制在工作区内。
- `shell` 优先使用 `bwrap` 沙箱；可用 `SPARK_SHELL_SANDBOX=off` 关闭。
- 批量删除/改名默认 dry-run，需 `confirm=true` 才执行。

## 工具

`shell` · `read` · `grep` · `ls` · `del` · `write` · `edit` · `diff` · `plan` · `askuser` · `web_search` · `compact_context` · `analyze` · `show` · `office` · `rename` · `organize`

`show` 支持图片 / 网页链接 / 表格 / 思维导图等展示卡片；`office` 读写 xlsx/csv/docx。

## 主要 API

- `GET /api/health` — 服务与模型状态（含 `llama_bin_resolved`、`model_dir`）
- `GET /api/settings` · `POST /api/settings/apply` — 运行设置（含 `llama_bin`、`model_dir`）
- `POST /api/chat` — SSE 对话（支持附件；图片在视觉模型下直传）
- `POST /api/llama/stop` · `POST /api/llama/start` — 手动停/启模型（停止后不自动拉起）
- `POST /api/workspace` — 切换工作区

## 环境变量一览

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SPARK_LLAMA_BIN` | 自动 | llama-server 路径 |
| `SPARK_MODEL_DIR` | `~/models` | GGUF 目录 |
| `SPARK_MODEL` | 目录内第一个 gguf | 默认模型 |
| `SPARK_LLAMA_PORT` | `8080` | 模型端口 |
| `SPARK_WEB_PORT` | `8787` | Web 端口 |
| `SPARK_WEB_HOST` | `127.0.0.1` | Web 监听 |
| `SPARK_WORKSPACE` | 项目目录 | 工具根目录 |
| `SPARK_LLAMA_BASE_URL` | `http://127.0.0.1:8080/v1` | OpenAI 兼容地址 |
| `SPARK_SHELL_SANDBOX` | `auto` | `auto` / `on` / `off` |
| `SPARK_SHELL_NETWORK` | `1` | shell 是否联网 |

## 停止 / 重启

- 顶栏 **停止模型 / 启动模型**：手动控制 llama.cpp；停止后看门狗不会自动拉起。
- `Ctrl+C`（`./start.sh` 前台）：停 Web 与脚本拉起的 llama。
- systemd 用户单元示例见仓库 `deploy/`（可选）。

## 许可证

见仓库 LICENSE（若未附则仅供个人使用）。
