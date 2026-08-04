# 环境变量与运行配置

本文是配置参考，不替代[从零部署指南](deployment.md)。聊天功能的最小配置只有飞书 App ID 和 App Secret，不需要任何 MCP 变量。

## 配置加载顺序

服务启动时读取项目根目录 `.env`，但同名的进程环境变量优先，不会被 `.env` 覆盖：

```text
进程环境变量 > 项目根目录 .env > config.json 中的默认值
```

修改 `.env` 或 `config.json` 后必须重启正在运行的服务。

## `.env`：本机凭证

初始化：

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

不要用带真实 Secret 的 `echo ...` 命令创建文件，因为命令可能进入 shell 历史。最小配置：

```dotenv
FEISHU_APP_ID=
FEISHU_APP_SECRET=

FEISHU_BITABLE_APP_TOKEN=
FEISHU_BITABLE_TABLE_ID=
```

| 变量 | 必需 | 说明 |
|---|---|---|
| `FEISHU_APP_ID` | 是 | 飞书企业自建应用 App ID |
| `FEISHU_APP_SECRET` | 是 | 飞书企业自建应用 App Secret |
| `FEISHU_BITABLE_APP_TOKEN` | 否 | 可选多维表格 App Token |
| `FEISHU_BITABLE_TABLE_ID` | 否 | 可选多维表格 Table ID |

只有两个 `FEISHU_BITABLE_*` 同时非空时才启用多维表格同步。第一次部署应保持为空。

检查配置而不显示凭证：

```bash
./start.sh doctor
git status --short
```

诊断应显示 `.env` 权限 `0600`，Git 状态中不应出现 `.env`。`.gitignore` 会排除它，但仍不要执行 `git add -f .env`。

## `config.json`：可提交的运行参数

`config.json` 只保存非敏感参数和 `${ENV_VAR}` 引用。主要字段：

| 字段 | 默认值 | 说明 |
|---|---:|---|
| `cold_start_wait_sec` | `6` | Codex 冷启动额外等待时间 |
| `feishu_ws_reconnect_interval_sec` | `10` | 长连接重连间隔 |
| `scheduler_poll_sec` | `2` | 调度器扫描间隔 |
| `scheduler_max_workers` | `2` | 定时任务最大并发数 |
| `scheduler_task_timeout_sec` | `1800` | 单个定时任务超时秒数 |
| `codex_reply_timeout_sec` | `7200` | 飞书等待单轮回复的最大秒数 |
| `hub_reconcile_interval_sec` | `30` | Hub 本机资源对账间隔 |
| `routing.auto_create` | `true` | 新会话是否自动创建工作区 |

`engines.codex.command` 默认包含跳过审批和沙箱的参数，以支持无人值守运行。这是明确的高风险选择；修改前先阅读[安全与隐私](security.md)，不要在不理解影响时增加系统权限或扩大工作目录。

修改 JSON 后检查格式：

```bash
./start.sh doctor --offline
```

## 每个飞书会话的配置

首次收到新会话消息时会创建：

```text
bots/auto_<chat_id>/
├── agent.json       # 模型、Effort、资源和保留周期
├── AGENTS.md        # 会话工作规则
├── memory/          # 长期摘要
├── incoming/        # 收到的附件
├── workspace/       # Agent 生成的文件
└── scheduled_tasks/ # 定时任务运行日志
```

模型、reasoning effort、Skill/MCP 和保留周期主要通过飞书控制面板管理。附件与任务日志默认保留 30 天；`0` 表示不自动清理。建议使用 `/privacy` 查看和调整，不要在任务运行中手改 `agent.json`。

项目根目录的 `registry.json` 保存真实飞书会话 ID 到工作区的映射。它会自动创建，只留在本机，不应提交。

## 可选 MCP 的环境变量

公开版本不附带或默认启用任何 MCP，所以 `.env.example` 不包含 MCP、浏览器或个人宿主平台变量。

如果你在本机创建 `hub/mcp/*.json`，可使用 `${ENV_VAR}` 引用私密值，然后只在本机 `.env` 加入该 MCP 实际需要的变量，例如：

```dotenv
MCP_EXAMPLE_URL=https://example.internal/mcp
MCP_EXAMPLE_AUTHORIZATION=Bearer your-token
```

个人 MCP 配置文件会被 Git 忽略。不要把完整 Bearer Token、私有 URL 或个人服务名称写进准备公开提交的 JSON、README 或日志。

## 配置修改后的验证

```bash
./start.sh doctor
./start.sh tmux restart
./start.sh tmux status
./start.sh tmux logs
```

如果使用的是前台、nohup 或 autostart，请改用对应方式重启；不要同时启动多个模式。完整命令见[运行与维护](operations.md)。
