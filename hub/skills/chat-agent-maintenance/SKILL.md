---
name: chat-agent-maintenance
description: 维护当前 chat-agent 会话工作目录和项目配置时使用。覆盖项目文件架构、incoming 附件、memory 记忆、agent 设置、skill/MCP 同步、默认系统约定和安全迁移。
---

# Chat Agent 维护 Skill

你运行在某个飞书会话的工作目录里。这个 skill 用于理解和维护 chat-agent 会话目录，不负责具体业务问答，也不负责飞书主动发送。

## 工作目录架构

每个飞书会话对应一个独立工作目录，通常在 `bots/<name>/` 下。当前 shell 的 cwd 就是该会话目录。

常见文件和目录：

- `agent.json`：当前会话配置，如引擎、模型、记忆模式、回复策略、返回格式、过程模式。
- `workspace/`：agent 在对话中主动生成的报告、脚本、数据、图片、导出文件和临时产物。除非用户明确指定其他路径，所有新产物都放这里。
- `memory/`：长期保留的会话记忆目录。
- `incoming/`：用户上传或发送到飞书的图片、文件、音频、媒体等附件。
- `incoming/<message_id>/`：server 下载某条附件消息时保存的原始文件目录。
- `scheduled_tasks/`：chat-agent 持久化的定时任务和运行记录；只通过 `scheduled-task` Skill/CLI 管理，不要直接改 JSON。
- `AGENTS.md`：Codex 会话指令。带 `<!-- chat-agent:fresh -->` 的文件由 chat-agent 自动维护。
- `.codex/skills/`：Codex 可用 skill。
- `.codex/skills_disabled/`：当前群暂存禁用的专用 Skill。
- `.codex/mcp/<name>/`：Codex 通过对话生成的专用 MCP bundle，可包含配置、源码、依赖和测试。
- `.mcp.json`：chat-agent 管理的 MCP 真相源，用于生成 Codex 配置。
- `.codex/config.toml`：Codex MCP 配置。
- `.chat_agent_system_defaults.json`：chat-agent 默认 skill/MCP 同步状态。

## 附件处理

- 单独发送的图片/文件会先保存到 `incoming/<message_id>/`，不一定立刻进入模型对话。
- 用户随后发文本时，server 会在 prompt 的 `【附件】` 段落中逐行给出同一发送人的附件绝对路径。
- 同一条飞书富文本消息里包含“图片 + 文字”时，也会直接注入这些绝对路径并触发对话。
- 直接读取 `【附件】` 下列出的文件；不要猜测、扫描或改写附件路径。

## 生成文件

- 默认把本轮对话产生的文件写入 `workspace/`，可以按任务再建子目录，如 `workspace/report/`、`workspace/assets/`、`workspace/tmp/`。
- 会话根目录只放 chat-agent 运行配置和系统目录；不要把报告、脚本、下载副本、导出结果直接放在根目录。
- `incoming/` 只放用户上传/飞书下载的原始附件；不要把 agent 新生成的文件写入 `incoming/`。
- `memory/` 只放长期记忆和滚动摘要；不要把普通任务产物写入 `memory/`。
- 用户要求“发送给我 / 发文件 / 发图片”时，先在 `workspace/` 生成文件，再使用独立的 `feishu-send` skill 发送。

## 记忆文件

每个会话的记忆都放在工作目录的 `memory/` 下，并且无论当前是全量记忆还是提炼记忆，都必须持续保留：

- `memory/MEMORY.md`：长期稳定记忆主索引。记录用户偏好、重要事实、长期目标、稳定约束；也可以索引 `memory/` 下的其他专题文件。
- `memory/history_chat.md`：对话滚动摘要。记录近期讨论、阶段性结论、待跟进事项；不要保存逐字聊天。

维护规则：

- 用户明确说“记住 / 写入记忆 / 以后都按这个来”时，优先更新 `memory/MEMORY.md`。
- 普通阶段性讨论、临时结论、待办和最近上下文，写入 `memory/history_chat.md`。
- 不要删除或清空这两个文件；需要重构时先保留原信息，再做归并。
- 如需新增专题记忆文件，放在 `memory/` 下，并在 `memory/MEMORY.md` 里添加索引。

## 项目维护

- 当前会话配置在 `agent.json`；不要手改不理解的字段。
- 当前会话的 Codex 指令在 `AGENTS.md`。带 `<!-- chat-agent:fresh -->` 的文件由 chat-agent 自动维护。
- 默认系统 Skill 从 Hub 同步到 `.codex/skills/<name>/`。
- 创建或更新 Skill/MCP 时使用 `hub-publish`：默认保留为当前群专用，只有用户明确要求时才升级为通用并发布。
- Skill 新增和修改由 Codex 自动检测，通常无需重启。
- MCP 新增和修改只更新配置并标记待载入；对话内不得请求或执行重启。由用户打开控制面板后确认是否重启当前 Codex 会话。
- 用户要求“发送给我 / 发文件 / 发图片 / Markdown 卡片”时，使用独立的 `feishu-send` skill；不要在本 skill 里重复实现飞书通讯逻辑。

## 安全边界

- 不要删除用户文件、记忆文件、附件、已发布 skill/MCP，除非用户明确要求。
- 遇到旧路径 `MEMORY.md`、`memory/core.md`、`memory/history.md` 时，只做迁移或保留提示；新的权威路径是 `memory/MEMORY.md` 和 `memory/history_chat.md`。
