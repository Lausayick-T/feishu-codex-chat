---
name: feishu-send
description: 通过飞书主动发送文本、文件、图片、卡片、Markdown 卡片或 HTML 渲染卡片给当前会话。当用户明确要求“发送给我”“发文件/图片”“上传给我”等，或要求生成报告、文档、表格、演示文稿、图片等文件型交付物时使用；文件型交付物即使未明确要求发送，也要在生成后发送。发送新生成的交付物后，用极简语句说明内容和关注点；仅发送用户指定的现有文件时不做内容总结。需要澄清、确认或让用户选择时不要使用交互提问工具，直接用普通文本提问并等待用户下一条回复。
---

# 飞书主动发送 Skill

你运行在 chat-agent 为当前飞书会话创建的工作目录里。server 启动 agent 时会注入：

- `CHAT_ID`：当前飞书会话 ID。
- `CHAT_AGENT_HOME`：chat-agent 根目录。

把以下两类请求都视为发送任务：

- 用户明确要求“发送给我 / 发给我 / 以文件形式发送 / 传给我 / 发图片 / 发文件 / 上传到飞书”等。
- 用户要求生成报告、文档、表格、演示文稿、图片或其他文件型交付物。即使用户没有再说“发送”，也要在生成完成后用 helper 把最终产物发送到当前飞书会话。只供 agent 处理任务使用的临时文件或中间产物不属于交付物，无需发送。

不要只在最终回复里粘贴交付内容；应优先在当前会话的 `workspace/` 下创建本地文件、图片、卡片 JSON 或 Markdown 内容，然后用 helper 主动发送到飞书。

当你需要用户补充信息、确认方案、在多个选项中选择，或需要继续任务前先问几个问题时，不要调用 `ask` / `ask-file`，也不要使用任何交互式提问工具。直接在最终回复里用普通文本提出问题，并等待用户下一条消息回答。

## Helper 命令

公共 helper 位于 chat-agent 根目录：`scripts/feishu_send.py`。chat-agent 会向 Codex 注入 `CHAT_AGENT_HOME`，优先使用下面的命令。

Skill 目录内也保留兼容入口 `.codex/skills/feishu-send/feishu_send.py`，旧命令仍可转发到公共 helper。

发送文件：

```bash
python "$CHAT_AGENT_HOME/scripts/feishu_send.py" file workspace/file.md
```

发送图片：

```bash
python "$CHAT_AGENT_HOME/scripts/feishu_send.py" image workspace/image.png
```

发送一条额外文本消息：

```bash
python "$CHAT_AGENT_HOME/scripts/feishu_send.py" text "要发送的文本"
```

发送 Markdown 渲染卡片：

```bash
python "$CHAT_AGENT_HOME/scripts/feishu_send.py" md-file workspace/report.md
```

或直接发送短 Markdown 文本：

```bash
python "$CHAT_AGENT_HOME/scripts/feishu_send.py" md "## 标题\n\n正文"
```

发送 HTML 渲染卡片（把常见 HTML 标签转换成飞书卡片内容，不发送源码；不执行 JS，也不完整还原 CSS）：

```bash
python "$CHAT_AGENT_HOME/scripts/feishu_send.py" html-file workspace/page.html
```

或直接发送短 HTML：

```bash
python "$CHAT_AGENT_HOME/scripts/feishu_send.py" html "<h1>标题</h1><p>正文</p>"
```

发送自定义飞书交互式卡片 JSON：

```bash
python "$CHAT_AGENT_HOME/scripts/feishu_send.py" card-file workspace/card.json
```

`file` 默认用 `stream` 类型上传，常见 `.md/.txt/.json/.csv/.pdf` 都可直接发送。需要指定类型时可加：

```bash
python "$CHAT_AGENT_HOME/scripts/feishu_send.py" file workspace/report.pdf --file-type pdf
```

## 回复约定

- 发送新生成的报告或文件后，不要只说“已发送”。用 1-2 句最精简的话说明：这份产物讲了什么，以及最值得关注的结论、风险或异常；没有明显关注点时只说明内容，不要硬凑总结。
- 如果用户只是要求发送、转发或上传一个已存在的指定文件，只需简短确认发送结果和文件名，不要附加内容总结。
- 不要机械复述文件目录、生成过程、普通细节或大段摘要。
- 用户需要第一时间看到的风险、异常、决策结论或时效信息，应先用 `text` / `md` 主动发送简短提醒，再发送文件；最终回复再补充必要上下文。
- 需要澄清或让用户选择时，不要调用 helper；直接把问题写在最终回复里，等用户下一条消息回答。
- 当用户要求“预览/渲染 HTML”时，优先用 `html-file` / `html` 发送渲染卡片；不要把 HTML 源码直接粘贴到最终回复或普通文本消息里。
- 需要创建待发送文件时，默认写入 `workspace/`，不要把产物散落在会话根目录。
- 不要把文件全文或与已发送卡片完全相同的长内容重复粘贴到最终回复里；提炼后的摘要和关键注意点不属于重复。
- helper 会写入运行状态；server 只在 Codex 最终回复为空时使用发送确认兜底，不再覆盖正常终稿。
- 如果 helper 失败，要说明失败原因，并可给出本地文件路径。

## 收到附件时

server 会把用户发送的图片、文件、音频或媒体文件下载到当前工作目录的 `incoming/<message_id>/` 下。单独的附件消息不会立刻进入对话；当同一发送人随后发来文本请求时，prompt 会出现 `【附件】`，后面每行一个附件绝对路径。直接读取这些路径，不需要查找或生成 JSON 清单。
