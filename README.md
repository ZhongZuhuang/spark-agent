# Spark Agent

可移植的本机 **编码 Agent** Web 应用：

- **llama.cpp** 提供 OpenAI 兼容接口（`llama-server`）
- **Pydantic AI** 驱动工具循环（读文件、改代码、跑命令、搜网页…）
- **浏览器聊天界面**：Markdown、思考过程、工具调用卡片、计划面板、附件上传、多会话

本仓库为**可移植部署版**：不绑定某台开发机；`llama-server` 与 GGUF 模型目录均可自动探测或手动配置。

> 已剥离 OCR 工作台与 Computer-Use 工作台（见 CHANGELOG）。

---

## 功能一览

| 能力 | 说明 |
| --- | --- |
| 多会话聊天 | 侧栏历史、标题、自动保存 |
| Agent 工具循环 | shell / read / write / edit / office / web_search 等 |
| 附件 | 任意文件；视觉模型下图片可直传看图 |
| 展示卡片 | `show`：图片、网页链接、表格、思维导图 |
| Office | `office`：读写 xlsx / csv / docx |
| 模型能力识别 | 视觉 mmproj、MTP、扩散等标签与开关 |
| 上下文 | 真实 token 占用（llama `/slots`）、自动压缩 |
| 模型电源 | 顶栏手动停止 / 启动（停止后不自动拉起） |

---

## 环境要求

- **OS**：Linux 或 macOS（需 `bash`）
- **Python**：3.10+
- **llama.cpp**：已安装 `llama-server`（编译或包管理器）
- **模型**：至少一个聊天用 `.gguf`（可选同目录 `mmproj` 做视觉）
- **可选**：`bwrap`（shell 沙箱）；`pdftotext`（PDF 分析）

### 安装 llama.cpp（任选）

```bash
# 包管理器（发行版而定）
sudo apt install llama-cpp        # Debian/Ubuntu 示例
# 或从源码编译
git clone https://github.com/ggml-org/llama.cpp
cd llama.cpp && cmake -B build && cmake --build build -j --target llama-server
# 二进制一般在 build/bin/llama-server
```

把 `llama-server` 放进 `PATH`，或在设置里填写完整路径。

---

## 快速开始

```bash
git clone git@github.com:ZhongZuhuang/spark-agent.git
cd spark-agent

# 可选：指定模型目录与 llama 路径
export SPARK_MODEL_DIR="$HOME/models"
export SPARK_LLAMA_BIN="$(command -v llama-server || echo "$HOME/llama.cpp/build/bin/llama-server")"

./start.sh
```

浏览器打开：<http://127.0.0.1:8787>

| 地址 | 说明 |
| --- | --- |
| Web UI | `http://127.0.0.1:8787` |
| llama.cpp | `http://127.0.0.1:8080`（`--ui-only` 时不启动） |

### 常用启动方式

```bash
./start.sh                  # 起 Web + llama.cpp（按 settings.json）
./start.sh --ui-only        # 只起 Web，连已有模型服务
./start.sh --install-only   # 只装依赖
./start.sh --reset          # 清空会话后启动
./start.sh -h               # 帮助
```

连接已在跑的模型服务：

```bash
SPARK_LLAMA_BASE_URL=http://127.0.0.1:8080/v1 ./start.sh --ui-only
```

---

## 配置 llama-server 与模型目录

### 方式 A：环境变量（适合脚本 / CI）

```bash
export SPARK_LLAMA_BIN=/usr/bin/llama-server      # 或你的编译路径
export SPARK_MODEL_DIR="$HOME/models"             # 放 .gguf 的目录
export SPARK_MODEL="$HOME/models/qwen2.5-7b-q4.gguf"  # 可选默认模型
./start.sh
```

### 方式 B：Web 设置（推荐，会写入 `.run/settings.json`）

1. 打开 **设置**（顶栏）
2. **运行环境**：
   - **llama-server 可执行文件**：留空 = 自动探测；或填完整路径
   - **模型扫描目录**：该目录下的 `.gguf` 会出现在「选择模型文件」列表
3. 选择模型、上下文等推理参数
4. **保存并重启 llama.cpp**

### 自动探测顺序（llama-server）

1. 设置 `llama_bin` / 环境变量 `SPARK_LLAMA_BIN`
2. `PATH` 中的 `llama-server`
3. `/usr/bin/llama-server`、`/usr/local/bin/llama-server`
4. `~/llama.cpp/build/bin/llama-server`、`~/.local/bin/llama-server`

### 视觉模型

将 `mmproj-*.gguf` 与主模型放在**同一目录**（或按命名规则配对），设置里开启「视觉输入」后，聊天附件里的图片可直接进入模型。

---

## 界面说明

```
┌──────────┬────────────────────────────┬─────────────┐
│ 会话列表  │        聊天消息区           │  执行计划    │
│ 工作区   │  [附件] 输入框 [发送]        │  任务进度    │
└──────────┴────────────────────────────┴─────────────┘
     顶栏：设置 · 停止/启动模型
```

- **设置 → 运行环境 / 模型 / 模型能力 / 推理 / 性能**
- 顶栏 **停止模型 / 启动模型**：手动控制；停止后写入标记，守护进程不会自动拉起
- 附件：支持文档与图片；视觉模型下图片按多模态直传

---

## 工具列表

| 工具 | 作用 |
| --- | --- |
| `shell` | 工作区内执行命令（优先 bwrap 沙箱） |
| `read` / `write` / `edit` | 读写改 UTF-8 文本 |
| `grep` / `ls` / `diff` | 搜索、列目录、对比 |
| `del` / `rename` / `organize` | 删除 / 批量改名 / 整理（默认 dry-run） |
| `plan` / `askuser` | 多步计划、向用户提问 |
| `web_search` | 公开网页搜索 |
| `analyze` | Word / PPT / Excel / PDF / 文本摘要 |
| `office` | 结构化读写 xlsx / csv / docx |
| `show` | 展示图片、网页、表格、思维导图等 |
| `compact_context` | 压缩过长对话历史 |

---

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | Web / llama / 模型 / 能力状态 |
| GET | `/api/settings` | 读取设置（含 `llama_bin`、`model_dir`） |
| POST | `/api/settings/apply` | 保存并重启 llama.cpp |
| POST | `/api/chat` | SSE 流式对话（`message`、`session_id`、`attachments`） |
| POST | `/api/llama/stop` · `/api/llama/start` | 手动停止 / 启动主模型 |
| POST | `/api/workspace` | 切换工作区 `{"path":"/abs/dir"}` |
| GET | `/api/sessions` · `/api/session` | 会话列表 / 详情 |

`POST /api/llama/stop`、`POST /api/llama/start`：手动停启主模型；**停止后不会被自动拉起**，直到你再点「启动模型」或调用 start。

SSE 事件：`loop_start`、`loop_end`、`plan_preview`、`ask_user`、`delta`、`reasoning_delta`、`tool_call`、`tool_result`、`done`、`error`、`finish`。

---

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SPARK_LLAMA_BIN` | 自动 | `llama-server` 可执行文件路径 |
| `SPARK_MODEL_DIR` | `~/models` | GGUF 扫描目录 |
| `SPARK_MODEL` | 目录内第一个 gguf | 默认模型文件 |
| `SPARK_LLAMA_PORT` | `8080` | 模型端口 |
| `SPARK_LLAMA_BASE_URL` | `http://127.0.0.1:8080/v1` | OpenAI 兼容 Base URL |
| `SPARK_WEB_HOST` | `127.0.0.1` | Web 监听地址 |
| `SPARK_WEB_PORT` | `8787` | Web 端口 |
| `SPARK_WORKSPACE` | 项目目录 | 工具可读写根目录 |
| `SPARK_CONTEXT_SIZE` | `65536` | 默认上下文（设置可改） |
| `SPARK_GPU_LAYERS` | `18` | GPU 层数（设置可改） |
| `SPARK_SHELL_SANDBOX` | `auto` | `auto` / `on` / `off` |
| `SPARK_SHELL_NETWORK` | `1` | shell 是否允许网络 |
| `SPARK_MODEL_NAME` | `Spark` | 对外模型别名（OpenAI `model` 字段） |

运行时可改项以 `.run/settings.json` 与 Web 设置为准（优先于部分启动默认值）。

---

## 目录结构

```
spark-agent/
├── app/                 # FastAPI + Agent 后端
│   ├── main.py          # 路由、llama 启停、附件
│   ├── agent.py         # Pydantic AI 工具注册与流式运行
│   ├── config.py        # 路径 / 设置 / 模型能力识别
│   ├── tools.py         # shell、office、show 等
│   ├── sessions.py      # 会话持久化
│   └── context.py       # 历史压缩与 token 估算
├── static/              # 前端（无构建步骤）
├── start.sh             # 一键启动
├── requirements.txt
└── .run/                # 运行时（gitignore）：settings.json、会话、日志
```

---

## 工作区与安全边界

- 工具路径限制在当前**工作区**内；侧栏可切换已存在的绝对路径。
- `shell` 默认尝试 `bwrap`：根只读、工作区可写；`SPARK_SHELL_SANDBOX=off` 可关闭。
- 删除 / 改名 / 整理默认只预览，需 `confirm=true` 才真正执行。
- 这**不是**完整容器安全方案；请勿对不可信输入直接开放重要目录。

---

## 故障排查

| 现象 | 处理 |
| --- | --- |
| 找不到 llama-server | 设置里填完整路径，或 `export SPARK_LLAMA_BIN=...` |
| 模型列表为空 | 检查「模型扫描目录」是否含 `.gguf` |
| 启动 llama 超时 | 看 `.run/llama.log`；GPU 显存不足时把 GPU Layers 调低 |
| 图片不能看 | 需视觉模型 + 同目录 mmproj，并开启视觉输入 |
| 点了停止模型后不会自己起来 | **预期行为**；点「启动模型」或 `POST /api/llama/start` |
| 端口被占用 | `SPARK_WEB_PORT` / `SPARK_LLAMA_PORT` 换端口 |
| 只有界面没有模型 | `./start.sh --ui-only` 并设置 `SPARK_LLAMA_BASE_URL` |

日志：

```bash
# start.sh 前台时直接打在终端
# 若自行 nohup / systemd：
tail -f .run/llama.log
```

---

## 与「开发机专用版」的差异

| | 本仓库（Portable） | 专用版 |
| --- | --- | --- |
| llama 路径 | 自动 + 可手填 | 常写死本机路径 |
| 模型目录 | 可自定义 | 绑定本机目录 |
| OCR / Computer | **无** | 有 |
| 目标 | 任意机器可部署 | 单机定制 |

---

## 许可证

若仓库未附带 LICENSE，默认仅供个人学习与内部使用；商用前请自行补充开源协议并确认模型许可。
