# 运行与维护

## 启动方式

四种方式只选择一种，避免重复消费飞书事件。

```bash
# 前台
./start.sh

# tmux
./start.sh tmux start
./start.sh tmux status
./start.sh tmux logs
./start.sh tmux restart
./start.sh tmux stop

# nohup
./start.sh nohup start
./start.sh nohup status
./start.sh nohup logs

# macOS LaunchAgent / Linux systemd user
./start.sh autostart install
./start.sh autostart status
./start.sh autostart restart
./start.sh autostart uninstall
```

统一 tmux 会话名为 `feishu-codex-chat`，Server 与 Scheduler 分别位于独立窗口。脚本发现旧的 `feishu-server`、`chat-agent-server` 或 `feishu-scheduler` 时会阻止重复启动。

## 群内命令

| 命令 | 作用 |
|---|---|
| `/help` | 打开控制面板 |
| `/setting` | 打开控制面板 |
| `/status` | 查看飞书、Codex、调度器和资源载入状态 |
| `/privacy` | 查看保留策略，清理当前会话本地数据 |
| `/sync` | 手动同步多维表格与本机资源 |

停止、模型切换、记忆设置、Skill/MCP 和定时任务管理主要通过控制面板完成。

## 健康检查

- `state/server.lock`：Server 单实例锁。
- `state/scheduler.lock`：Scheduler 单实例锁。
- `state/scheduler.heartbeat.json`：调度器状态和最近心跳。
- `state/scheduler.db`：任务和运行历史。
- `tmux list-sessions`：会话进程状态。

## 备份

需要备份的本机数据取决于业务要求：

- `.env`：应进入安全的密钥管理系统，不要进入普通文件备份或 Git。
- `registry.json`：会话路由。
- `state/scheduler.db`：定时任务。
- `bots/auto_*/memory/`：长期记忆。
- `bots/auto_*/workspace/`：用户要求保留的产物。

附件、stdout/stderr、Codex rollout 和临时图片通常应设置保留期限，而不是无限备份。

## 升级

升级前先备份必要数据，然后：

```bash
uv sync --locked
uv run python -m unittest discover -s tests -v
uv run python scripts/audit_public.py
./start.sh tmux restart
```

不要同时保留旧启动会话与统一服务。Codex 会话重启可能丢失未提炼的临时上下文，重要信息应先写入 `memory/`。
