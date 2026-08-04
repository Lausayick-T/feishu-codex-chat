# 故障排查

先不要反复重启。保留当前错误现场，依次执行：

```bash
./start.sh doctor
./start.sh tmux status
./start.sh tmux logs
codex login status
```

如果当前使用前台、nohup 或 autostart，请将 tmux 命令替换为对应模式。分享日志前务必删除 App ID、Token、群/用户 ID、本机路径、聊天正文和附件内容。

## `doctor` 有失败项

| 失败项 | 处理方式 |
|---|---|
| Python 版本低于 3.10 | 安装 Python 3.10+，重新执行 `uv sync --locked` |
| 找不到 `uv` / `tmux` / `codex` | 安装依赖；若已经安装，重新打开终端并检查 `PATH` |
| Codex 未登录 | 用运行服务的同一个系统账户执行 `codex login` |
| `.env` 不存在 | `cp .env.example .env && chmod 600 .env`，再用编辑器填写 |
| App ID / Secret 未配置 | 检查变量名和等号两侧，不要把示例说明文字当成真实值 |
| 飞书凭证验证失败 | 重新从同一个应用复制 App ID 和 App Secret；若曾泄露，先轮换 Secret |
| 飞书在线验证网络失败 | 检查 DNS、代理和防火墙；`--offline` 只能跳过验证，不能让服务离线运行 |
| 机器人能力未确认 | 确认应用已添加机器人能力、可用范围正确，并已发布新版本 |

黄色的“Codex 权限”提醒来自默认无人值守参数。它不会阻止启动，但意味着必须使用低权限账户并限制可访问目录。

## 服务启动后立即退出

先前台运行以看到第一条异常：

```bash
./start.sh tmux stop
./start.sh
```

常见原因：

- `.venv` 不完整：重新执行 `uv sync --locked`。
- `config.json` 被改坏：执行 `./start.sh doctor --offline` 检查 JSON。
- 同一项目已由另一种模式运行：分别检查 `tmux status`、`nohup status` 和 `autostart status`。
- 残留旧 tmux 会话：执行 `tmux list-sessions`，确认后用对应旧项目自己的停止命令处理；不要盲目结束不认识的用户会话。

## 机器人完全收不到消息

按顺序确认：

1. Server 仍在运行，日志中没有持续重连或鉴权错误。
2. 飞书“事件配置”选择了长连接。
3. 已添加 `im.message.receive_v1`。
4. 私聊权限 `im:message.p2p_msg:readonly` 和群 `@` 权限 `im:message.group_at_msg:readonly` 已批准。
5. 权限和事件所在的新应用版本已经发布。
6. 测试用户在应用可用范围内，机器人已加入目标群。
7. 多人群中使用了 `@机器人`。

如果后台提示没有建立长连接，保持 `./start.sh` 前台运行，再回后台刷新并保存长连接配置。

## 能收到消息，但不能回复

重点检查发送权限：

- 推荐方案需要 `im:message`。
- 细粒度方案至少需要 `im:message:send_as_bot`；更新进度卡还需要权限清单允许的更新能力。
- 权限批准后是否发布了新版本。
- 日志中的飞书错误码，但分享时不要粘贴请求头或完整响应中的身份信息。

详见[飞书权限清单](feishu-permissions.md)。

## 私聊正常，群聊不回复

- 确认机器人已在该群，且目标成员在应用可用范围内。
- 多人群必须 `@机器人`，这是预期行为。
- 检查 `im:message.group_at_msg:readonly` 和 `im.message.receive_v1`。
- 如果希望单真人群免 `@`，还需要敏感权限 `im:message.group_msg` 与 `im:chat.members:read`。

当群成员读取失败时，项目会保守地按多人群处理，只响应 `@机器人`，避免误触发。

## 卡片能显示，但按钮无反应

消息事件和卡片回调是两套配置。进入“事件与回调 → 回调配置”：

1. 选择“使用长连接接收回调”。
2. 添加 `card.action.trigger`。
3. 发布包含该配置的新版本。
4. 重启服务并重新打开 `/help`，不要只点击很久以前的旧卡片。

## 图片或文件处理失败

按数据方向区分：

- 机器人上传或发送文件失败：检查 `im:resource` 和消息发送权限。
- 机器人无法下载用户发来的附件：检查 `im:message` 或 `im:message:readonly`。
- 群文件失败但私聊成功：确认机器人仍在该群并有对应会话访问权。
- 大文件或特殊格式失败：先用小型 PNG 或 TXT 验证基础链路，再查看日志中的平台大小/类型限制。

收到的附件保存在当前会话 `incoming/`，默认按保留策略清理；不要把该目录提交或打包进公开 Issue。

## Codex 一直显示启动、无回复或登录失效

必须用“运行服务的同一个系统账户”检查：

```bash
codex login status
codex --version
```

然后在一个临时、无敏感文件的目录手动启动 Codex，确认账号和网络可用。若登录失效，执行 `codex login` 后重启服务。

其他可能原因：

- Codex 服务网络不可达或代理只配置在交互式 shell，没有进入后台服务环境。
- 当前回合耗时超过 `codex_reply_timeout_sec`。
- 运行账户无权访问会话工作目录或 tmux。
- 模型名或本地 `agent.json` 配置无效；从飞书控制面板切回可用模型。

不要把 Codex 本机凭证文件、完整 rollout 或含用户正文的 JSONL 发到公开 Issue。

## 定时任务不执行

```bash
./start.sh tmux status
./start.sh tmux logs
```

确认 `scheduler` 窗口存在，并在飞书 `/status` 中查看心跳。还应检查：

- 机器时区是否符合预期。
- 任务是否启用、下一次执行时间是否正确。
- `scheduler_max_workers` 是否长期占满。
- 运行账户的 Codex 登录是否仍有效。
- 任务依赖的本机 Skill/MCP 是否仍存在。

定时任务状态在 `state/scheduler.db`，不要在服务运行时直接手工修改数据库。

## 长连接反复断开或重复消费

- 确认只运行一种启动模式，且没有另一台机器使用同一 App ID 启动同一服务。
- 检查网络代理是否支持到飞书消息长连接域名的 WebSocket/WSS 连接。
- 检查防火墙、公司代理、DNS 和系统时间。
- 查看日志是偶发重连还是持续鉴权失败；偶发网络重连通常会自动恢复。

如果部署过旧版本，`tmux list-sessions` 可能看到旧服务会话。只停止已确认属于本项目的会话，不要结束其他用户或其他项目的 tmux。

## 升级后异常

```bash
git status --short
uv sync --locked
./start.sh doctor
uv run python -m unittest discover -s tests -v
```

确认没有把旧版源码、虚拟环境或多个服务模式混用。需要回退时，应先备份 `.env`、`registry.json`、`state/scheduler.db` 和必要会话数据，再按版本说明处理；不要用破坏性 Git 命令覆盖未确认的本机数据。

## 仍无法解决

提交 Issue 时只提供：

- 操作系统类型与版本，不含用户名和个人路径。
- Python、uv、tmux、Codex CLI 的版本号。
- 从哪一步开始失败、预期行为和实际行为。
- 已脱敏的最小错误码或堆栈。

不要提供 App ID、App Secret、Token、真实群/用户 ID、聊天正文、附件、`.env`、`registry.json`、MCP 配置或 Codex 会话记录。
