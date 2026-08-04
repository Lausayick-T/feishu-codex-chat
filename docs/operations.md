# 运行与维护

第一次安装请先完成[从零部署指南](deployment.md)。本文用于部署成功后的日常启停、日志、备份和升级。

## 启动方式只能选一种

| 方式 | 适用场景 | 启动命令 |
|---|---|---|
| 前台 | 首次部署、观察实时错误 | `./start.sh` |
| tmux | 日常手动后台运行，推荐先用 | `./start.sh tmux start` |
| nohup | 不使用 tmux 管理服务时 | `./start.sh nohup start` |
| autostart | 登录或开机后自动恢复 | `./start.sh autostart install` |

不要同时使用多个方式，否则会重复消费飞书事件。控制脚本会尽量检测旧进程和旧 tmux 会话并拒绝重复启动。

## 首次运行

```bash
./start.sh doctor
./start.sh
```

保持前台运行并完成飞书验收。确认能收消息、发送回复和处理卡片按钮后，按 `Ctrl-C` 一起停止 Server 与 Scheduler，再切换后台模式。

## tmux：推荐的日常方式

```bash
./start.sh tmux start
./start.sh tmux status
./start.sh tmux logs
./start.sh tmux restart
./start.sh tmux stop
```

统一会话名为 `feishu-codex-chat`，包含 `server` 与 `scheduler` 两个窗口。`logs` 会抓取两个窗口最近的输出，不需要手动进入 tmux。

正常 `status` 应显示会话正在运行，并列出两个窗口。如果会话启动后立即退出，先看 `logs`；没有日志时改用前台 `./start.sh` 获取首个异常。

## nohup

```bash
./start.sh nohup start
./start.sh nohup status
./start.sh nohup logs
./start.sh nohup restart
./start.sh nohup stop
```

PID 和 stdout/stderr 位于 `state/services/`。这些都是本机运行数据，已被 Git 忽略。

## 自动启动

macOS 使用当前用户的 LaunchAgent，Linux 使用当前用户的 systemd user service；两者最终都启动统一 tmux 会话。

安装前先停止其他模式：

```bash
./start.sh tmux stop
./start.sh nohup stop
./start.sh autostart install
./start.sh autostart status
```

常用命令：

```bash
./start.sh autostart logs
./start.sh autostart restart
./start.sh autostart uninstall
```

Linux 若要求用户退出登录后仍运行，需要管理员按本机策略启用 user lingering，例如 `loginctl enable-linger <运行账户>`。这会改变主机登录会话行为，应由主机管理员评估后执行；项目不会自动修改它。

可以先只生成配置预览，不安装：

```bash
./start.sh --dry-run autostart install
```

## 固定健康检查

建议每次部署或配置变更后执行：

```bash
./start.sh doctor
./start.sh tmux status
./start.sh tmux logs
```

再在飞书发送 `/status`。本机关键状态：

| 路径 | 内容 |
|---|---|
| `state/server.lock` | Server 单实例锁 |
| `state/scheduler.lock` | Scheduler 单实例锁 |
| `state/scheduler.heartbeat.json` | 调度器最近心跳 |
| `state/scheduler.db` | 定时任务与运行历史 |
| `state/services/` | nohup/autostart 日志和 PID |

锁文件存在不等于进程一定健康，应以 `status`、日志和飞书 `/status` 综合判断。

## 群内运维命令

| 命令 | 作用 |
|---|---|
| `/help`、`/setting` | 打开控制面板 |
| `/status` | 查看飞书、Codex、调度器和资源状态 |
| `/privacy` | 查看保留策略并清理当前会话数据 |
| `/sync` | 手动同步可选多维表格配置 |

停止当前 Codex 回合、模型切换、记忆设置、Skill/MCP 和任务管理主要通过控制面板完成。

## 安全备份

根据业务需要选择备份内容：

- `.env`：放入专用密钥管理或加密备份，不要进入普通 Git 或共享网盘。
- `registry.json`：会话路由映射，包含真实飞书会话 ID。
- `state/scheduler.db`：定时任务和运行历史。
- `bots/auto_*/memory/`：长期摘要。
- `bots/auto_*/workspace/`：用户明确要求保留的产物。

`incoming/` 附件、日志、Codex rollout 和临时图片通常应该设置保留周期，而不是永久备份。恢复时保持原文件权限，并先离线验证再启动服务。

## 升级

先查看变更说明并备份必要的本机数据。使用哪种服务模式，就先停止哪一种。以 tmux 为例：

```bash
./start.sh tmux stop
git status --short
git pull --ff-only
uv sync --locked
./start.sh doctor
uv run python -m unittest discover -s tests -v
uv run python scripts/audit_public.py --current-only
./start.sh tmux start
./start.sh tmux status
./start.sh tmux logs
```

如果 `git status` 显示你自己的源码改动，不要直接覆盖；先备份、提交到私有分支或人工合并。`.env`、会话目录和状态文件不应出现在 Git 状态中。

升级后在飞书重新验证 `/help`、卡片按钮、普通问题和 `/status`。Codex 会话重启可能丢失尚未提炼的临时上下文，重要信息应先写入会话记忆或外部业务系统。

## 停用

先停止当前运行方式：

```bash
./start.sh tmux stop
# 或：./start.sh nohup stop
# 或：./start.sh autostart uninstall
```

然后在飞书开发者后台下线应用或移除机器人。删除 `.env`、会话数据和备份属于不可恢复操作；确认保留要求后再手动处理，项目不会自动删除它们。

遇到异常时使用[故障排查](troubleshooting.md)。
