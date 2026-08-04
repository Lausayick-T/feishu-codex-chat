# Feishu Codex Chat

把飞书私聊或群聊接入 Codex CLI。每个飞书会话都有独立的工作目录、Codex 会话、附件、记忆与定时任务；公开仓库不包含任何个人会话、MCP 配置或真实凭证。

> 第一次部署请直接阅读 **[从零部署指南](docs/deployment.md)**。它按实际操作顺序覆盖依赖安装、Codex 登录、飞书应用创建、权限、长连接、启动和验收。

## 适用环境

| 项目 | 要求 |
|---|---|
| 操作系统 | macOS 或 Linux；不支持原生 Windows |
| Python | 3.10 或更高版本 |
| 运行依赖 | `uv`、`tmux`、Git、Codex CLI |
| 飞书 | 可创建企业自建应用，并能发布或请管理员审核 |
| Codex | 运行服务的系统账户已完成 `codex login` |

部署后的机器需要持续在线，并能访问飞书开放平台与 Codex 使用的网络服务。

## 最短部署路径

```bash
git clone https://github.com/Lausayick-T/feishu-codex-chat.git
cd feishu-codex-chat
uv sync --locked
cp .env.example .env
chmod 600 .env
```

在 `.env` 中填写飞书应用的 `FEISHU_APP_ID` 和 `FEISHU_APP_SECRET`，然后执行：

```bash
./start.sh doctor
./start.sh tmux start
./start.sh tmux status
./start.sh tmux logs
```

这几条命令只是本机部分。飞书后台还必须完成机器人能力、API 权限、消息事件、卡片回调和版本发布；请不要跳过[完整部署流程](docs/deployment.md)。

## 功能

- 飞书长连接接收消息，无需公网回调服务器。
- 按 `chat_id` 隔离多个 Agent 工作区。
- 支持文本、富文本、图片、文件、媒体和音频。
- 单真人群直接回复；多人群仅响应 `@机器人`。
- 交互卡片展示启动、执行、停止和最终结果。
- 每个群独立选择 Codex 模型与 reasoning effort。
- 摘要记忆、Skill/MCP Hub、主动发送文件和交互式澄清问题。
- SQLite 持久化定时任务，支持一次性、间隔、每日、每周和 Cron。
- 支持前台、tmux、nohup、macOS LaunchAgent 和 Linux systemd user。

## 使用入口

在飞书中发送：

| 命令 | 作用 |
|---|---|
| `/help` | 打开控制面板 |
| `/status` | 检查飞书链路、Codex、调度器和资源状态 |
| `/privacy` | 查看或清理当前会话的本地数据 |
| `/sync` | 手动同步可选的多维表格控制台 |

## 架构

```text
飞书消息 / 卡片回调
        │ 长连接
        ▼
     server.py ── chat_id → registry.json
        │
        ▼
 bots/auto_<chat_id>/ ── tmux ── Codex TUI
        │
        └── 附件、记忆、产物、任务日志（仅本机）

scheduler_service.py ── scheduler.db ── codex exec ── 飞书
```

核心代码：

- `server.py`：飞书事件与卡片回调入口。
- `utils/session.py`：tmux 与 Codex 会话生命周期。
- `utils/workers.py`：进度卡和最终回复捕获。
- `utils/hub.py`：本机 Skill/MCP 装载与同步。
- `scheduler_service.py`：无人值守定时任务执行器。
- `scripts/servicectl.sh`：统一启动、停止和日志管理。

## 文档导航

建议按以下顺序阅读：

1. [从零部署指南](docs/deployment.md)：第一次安装的唯一主流程。
2. [飞书应用配置](docs/feishu-setup.md)：飞书后台逐页操作。
3. [飞书权限、事件与回调清单](docs/feishu-permissions.md)：按功能选择权限。
4. [环境变量与运行配置](docs/configuration.md)：配置项参考。
5. [故障排查](docs/troubleshooting.md)：按现象定位问题。
6. [运行与维护](docs/operations.md)：日常启停、自启动、备份和升级。
7. [消息、附件与数据处理](docs/data-flow.md)：数据如何流转和保留。
8. [安全与隐私](docs/security.md)：权限边界和公开发布检查。

## 安全边界

- `.env.example` 只有空字段；真实凭证必须写入被 Git 忽略的 `.env` 或进程环境。
- `.env`、`registry.json`、`.mcp.json`、`state/`、`bots/*`、个人 MCP、非内置 Skill、日志和数据库均不会提交。
- 默认 Codex 命令启用了跳过审批和沙箱的无人值守模式。只应使用专用低权限系统账户，并确保它不能读取 SSH 私钥、浏览器数据或其他业务凭证。
- 公开提交前运行 `uv run python scripts/audit_public.py`；如果凭证曾进入 Git 历史，仅删除文件不够，必须立即轮换。

详细说明见[安全与隐私](docs/security.md)。项目采用 [MIT License](LICENSE)，参与开发前请阅读[贡献指南](CONTRIBUTING.md)。

## 开发验证

```bash
uv sync --locked
uv run python -m unittest discover -s tests -v
uv run python -m compileall -q server.py scheduler_service.py hub_cli.py utils scripts
uv run python scripts/check_docs.py
uv run python scripts/audit_public.py
```
