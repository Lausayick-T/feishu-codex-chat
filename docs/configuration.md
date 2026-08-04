# 环境与配置

## 1. 系统要求

- Python 3.10 或更高版本。
- `uv`，用于创建虚拟环境和安装依赖。
- `tmux`，用于保持每个飞书会话的 Codex TUI。
- Codex CLI，且运行服务的系统账户已经完成登录。
- macOS 或 Linux。开机自启分别使用 LaunchAgent 和 systemd user。

```bash
uv sync --locked
codex doctor
tmux -V
```

配置完成后运行 `./start.sh doctor`，统一检查依赖、Codex 登录、`.env` 权限和飞书凭证。使用 `./start.sh doctor --offline` 可跳过飞书在线验证。

## 2. 本机密钥

复制环境变量模板：

```bash
cp .env.example .env
chmod 600 .env
```

至少填写：

```dotenv
FEISHU_APP_ID=你的飞书应用 App ID
FEISHU_APP_SECRET=你的飞书应用 App Secret
```

`.env` 由 `utils/config.py` 在进程启动时读取，已有系统环境变量优先，不会被 `.env` 覆盖。不要在值两侧添加多余空格。

多维表格是可选能力：

```dotenv
FEISHU_BITABLE_APP_TOKEN=
FEISHU_BITABLE_TABLE_ID=
```

只有两项同时存在时才启用多维表格同步。

## 3. 非敏感运行配置

`config.json` 可以安全提交，只保存运行参数和 `${ENV_VAR}` 引用。主要字段：

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `cold_start_wait_sec` | 6 | Codex 冷启动额外等待时间 |
| `codex_reply_timeout_sec` | 7200 | 飞书等待单轮回复的最大秒数 |
| `scheduler_poll_sec` | 2 | 调度器扫描间隔 |
| `scheduler_max_workers` | 2 | 定时任务最大并发数 |
| `scheduler_task_timeout_sec` | 1800 | 单个定时任务超时 |
| `hub_reconcile_interval_sec` | 30 | Hub 资源对账间隔 |
| `routing.auto_create` | `true` | 新群是否自动创建工作区 |

修改配置后需要重启 Server/Scheduler。模型、Effort 和每群资源配置保存在各工作区的 `agent.json` 中。

每个会话的 `agent.json` 还保存 `attachment_retention_days` 与 `task_log_retention_days`，默认均为 30 天，`0` 表示不自动清理。建议通过飞书 `/privacy` 页面修改，不要直接编辑运行中的会话文件。

## 4. MCP 环境变量

本机 `hub/mcp/*.json` 可以引用 `${ENV_VAR}`。装载资源时，系统会从 `.env` 或进程环境展开变量；`${MCP_BUNDLE_DIR}` 会保留给 bundle 路径解析。

公开版本不附带或默认启用任何 MCP，因此 `.env.example` 不罗列 MCP、浏览器或宿主平台变量。本机创建 MCP 后，仅将该配置引用的变量添加到 `.env`。`hub/mcp/*.json` 默认被 Git 忽略，避免把个人服务名称、地址或鉴权配置发布出去。

只填写实际启用的 MCP 变量。不要把 Bearer Token 拆到公开 JSON 中，完整鉴权值应放在 `.env`，例如：

```dotenv
MCP_EXAMPLE_URL=https://example.internal/mcp
MCP_EXAMPLE_AUTHORIZATION=Bearer your-token
```

## 5. 会话目录

```text
bots/auto_<chat_id>/
├── agent.json       # 每群设置
├── AGENTS.md        # 摘要记忆模式指令
├── memory/          # 长期摘要
├── incoming/        # 用户附件
├── workspace/       # Agent 生成文件
└── scheduled_tasks/ # 定时任务执行日志
```

`registry.json` 保存真实飞书会话 ID，因此只保留在本机。首次运行时可以不存在，系统会自动创建。

记忆和 `workspace/` 产物不会自动删除；用户可在飞书发送 `/privacy` 查看占用并通过二次确认手动清理。附件与定时任务日志默认保留 30 天，每小时至多执行一次过期清理。
