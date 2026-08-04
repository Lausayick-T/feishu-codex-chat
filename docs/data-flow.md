# 消息、附件与数据处理

## 消息入口

飞书通过 `im.message.receive_v1` 长连接事件把消息交给 `server.py`。服务首先按 `message_id` 做进程内去重，然后按 `chat_id` 查找 `registry.json`。

未登记会话在 `routing.auto_create=true` 时会从 `bots/_template/` 创建独立工作区。

## 回复策略

- 私聊始终响应。
- 群内只有一位真人时响应所有消息。
- 两位及以上真人时只响应 `@机器人` 的消息。
- 无法读取成员数量时采用保守策略，只响应 `@机器人`。

## 附件

支持 `image`、`file`、`media`、`audio`，以及富文本中的图片。

附件下载到：

```text
bots/<conversation>/incoming/<message_id>/
```

仅发送附件时，系统按 `chat_id + sender_open_id` 暂存，防止多人群误用其他人的文件。发送者后续发送文字后，附件绝对路径会随 prompt 一起交给 Codex。单真人群五分钟内没有补充文字时会发送处理提示。

## Codex 执行

每个会话对应一个 `feishu-<chat_id>` tmux session。输入通过 tmux bracketed paste 注入，避免多行文本被 shell 解释。

回复从 `~/.codex/sessions/**/rollout-*.jsonl` 中捕获。系统只选择 `session_meta.cwd` 与当前工作目录一致的会话，并识别 `final_answer` 或 `task_complete` 事件。

用户在执行中补充消息时，补充内容会送入同一当前任务；飞书侧新建一张进度卡承接后续状态和终稿。

## 记忆

- `resume`：依赖当前常驻 Codex TUI 的上下文；进程重启后不承诺恢复全部历史。
- `fresh`：额外维护 `memory/MEMORY.md` 与 `memory/history_chat.md`，用摘要跨重启恢复关键背景。

## 定时任务

任务定义和状态存放在 `state/scheduler.db`，使用 SQLite WAL。调度器原子领取到期任务，再通过独立的 `codex exec --ephemeral` 执行。

每次运行的 stdout、stderr 和最终输出保存在对应工作区的 `scheduled_tasks/runs/`，结果通过飞书发送。定时任务不会写入交互 TUI，因此不会与正在进行的聊天输入混合。

## 数据边界

以下内容只应存在于本机，并已加入 `.gitignore`：

- `.env`：凭证和 MCP 鉴权信息。
- `registry.json`：真实飞书 chat ID 与工作区映射。
- `.mcp.json`：当前会话或本机 MCP 配置。
- `state/`：数据库、锁、心跳、临时状态和日志。
- `bots/auto_*/`：聊天记忆、附件、业务文件和生成结果。
- `.codex/`、`.claude/`、`.catalysthub/`：本机 Agent 配置和会话数据。
