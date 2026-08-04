# Feishu Codex Chat

将飞书群聊或私聊连接到 Codex CLI。每个飞书会话拥有独立工作目录、常驻 tmux 会话、配置、记忆、附件和定时任务，并可按需装载共享 Skill 与 MCP。

## 功能

- 飞书长连接接收消息，无需公网回调服务器。
- 按 `chat_id` 隔离多个 Agent 工作区。
- 支持文本、富文本、图片、文件、媒体和音频。
- 单真人群直接回复，多人群仅响应 `@机器人`。
- 飞书交互卡片展示启动、执行、停止和最终结果。
- 首次使用引导、运行状态诊断和本机会话数据管理。
- 每个群独立选择 Codex 模型与 reasoning effort。
- 摘要记忆、Skill/MCP Hub、主动发送文件和交互式澄清问题。
- SQLite 持久化定时任务，支持一次性、间隔、每日、星期和五段 Cron。
- macOS LaunchAgent、Linux systemd user、tmux、nohup 和前台运行。

公开版本不附带或默认启用任何 MCP，`.env.example` 只保留本项目运行所需的飞书配置。个人 MCP、非内置 Skill 和各会话数据仅保存在本机，并由 `.gitignore` 阻止提交。

## 架构

```text
飞书消息 / 卡片回调
        │
        ▼
     server.py
        │ chat_id → registry.json
        ▼
 bots/<conversation>/
        │ tmux + bracketed paste
        ▼
   Codex 交互式 TUI
        │ ~/.codex/sessions/*.jsonl
        ▼
 进度卡刷新与最终回复

定时任务 → scheduler.db → codex exec --ephemeral → 飞书
```

核心代码：

- `server.py`：飞书事件入口与流程编排。
- `utils/session.py`：tmux 与 Codex 生命周期。
- `utils/workers.py`：进度和终稿捕获。
- `utils/hub.py`：Skill/MCP 安装、同步和发布。
- `utils/scheduled.py`：任务存储与调度计算。
- `scheduler_service.py`：无人值守任务执行器。
- `scripts/servicectl.sh`：统一服务管理。

## 快速开始

要求：Python 3.10+、[uv](https://docs.astral.sh/uv/)、tmux，以及已安装并登录的 Codex CLI。

```bash
git clone https://github.com/Lausayick-T/feishu-codex-chat.git
cd feishu-codex-chat
uv sync --locked
cp .env.example .env
chmod 600 .env
```

填写 `.env` 中的 `FEISHU_APP_ID` 与 `FEISHU_APP_SECRET`，然后按飞书文档创建应用、配置权限和长连接事件。

启动前先运行诊断；如暂时无法访问飞书开放平台，可添加 `--offline`：

```bash
./start.sh doctor
```

```bash
# 前台运行 Server 与 Scheduler，Ctrl-C 一起停止
./start.sh

# 或使用统一 tmux 服务
./start.sh tmux start
./start.sh tmux status
./start.sh tmux logs
```

首次收到新会话消息时，系统会创建 `bots/auto_<chat_id>/` 并写入本机 `registry.json`。这两类文件都不会提交到 Git。

在飞书中可随时发送：

- `/help`：打开控制面板。
- `/status`：查看飞书链路、Codex 会话、调度服务和资源状态。
- `/privacy`：查看或清理当前会话的记忆、附件、生成文件和任务日志。

## 文档

- [环境与配置](docs/configuration.md)
- [消息、附件与数据处理](docs/data-flow.md)
- [飞书应用操作流程](docs/feishu-setup.md)
- [飞书权限清单](docs/feishu-permissions.md)
- [安全与隐私](docs/security.md)
- [运行与维护](docs/operations.md)

## 测试

```bash
uv sync --locked
uv run python -m unittest discover -s tests -v
uv run python -m py_compile server.py scheduler_service.py hub_cli.py utils/*.py scripts/*.py
uv run python scripts/audit_public.py
```

## 安全提醒

- 真实凭证只放在 `.env` 或进程环境中，不要写入 JSON、Skill、MCP 配置或日志。
- `.env`、`registry.json`、`.mcp.json`、`state/`、整个 `bots/*` 会话目录、个人 Hub 资源和各类日志均被 Git 忽略。
- Codex 默认使用跳过审批与沙箱的参数。只应在专用低权限账户和受控工作目录中运行。
- 如果凭证曾进入 Git 历史或公开日志，仅删除文件不够，必须立即轮换凭证。

项目目前以内部自托管场景为目标。部署到共享服务器前，请先阅读[安全与隐私](docs/security.md)。

项目采用 [MIT License](LICENSE)。参与开发前请阅读[贡献指南](CONTRIBUTING.md)和[版本记录](CHANGELOG.md)。
