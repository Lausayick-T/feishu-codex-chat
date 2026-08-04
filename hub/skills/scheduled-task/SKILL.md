---
name: scheduled-task
description: 管理当前飞书对话的持久化定时任务。当用户要求定时、延时、每日、每周、周期性或 Cron 执行某项工作，或要查询、修改、暂停、恢复、立即运行、删除定时任务时使用。
---

# 定时任务

使用 chat-agent 提供的确定性 CLI 管理任务，不要自行创建 cron、`while sleep`、launchd 或额外 tmux。

## 入口

在当前对话工作目录中运行：

```bash
python "$CHAT_AGENT_HOME/scripts/schedule_task.py" <command> ...
```

server 已注入 `CHAT_ID` 和 `CHAT_AGENT_HOME`；不要手工猜测 chat_id。所有命令都返回 JSON，检查 `ok` 后再向用户确认。

## 创建

先明确任务内容和调度时间。只有会实质改变调度的关键信息缺失时才提问，例如“每天早上”却没有具体时间。默认时区是 `Asia/Shanghai`。

一次性：

```bash
python "$CHAT_AGENT_HOME/scripts/schedule_task.py" create \
  --name "会议提醒" --prompt "提醒用户参加项目会议" \
  --once "2026-07-22T14:30" --timezone "Asia/Shanghai"
```

每日或指定星期：

```bash
python "$CHAT_AGENT_HOME/scripts/schedule_task.py" create \
  --name "AI 新闻日报" --prompt "汇总过去 24 小时 AI 重要新闻，给出来源并发送 Markdown 报告" \
  --daily "08:30" --weekdays weekdays
```

星期可用 `all`、`weekdays`、`weekends` 或 `mon,wed,fri`。

固定间隔：

```bash
python "$CHAT_AGENT_HOME/scripts/schedule_task.py" create \
  --name "指标巡检" --prompt "检查指标并只在异常时明确警报" --interval "2h"
```

支持 `s/m/h/d/w` 及 `1h30m`。默认在一个完整间隔后首次执行；如需创建后立即跑一次，加 `--run-now`。

Cron：

```bash
python "$CHAT_AGENT_HOME/scripts/schedule_task.py" create \
  --name "月初报告" --prompt "生成上月总结" --cron "0 9 1 * *"
```

使用标准五段“分 时 日 月 周”。多行或引号复杂的任务内容先写入 `workspace/`，再传 `--prompt-file <path>`。

## 查询和管理

```bash
python "$CHAT_AGENT_HOME/scripts/schedule_task.py" list
python "$CHAT_AGENT_HOME/scripts/schedule_task.py" show <id-or-name>
python "$CHAT_AGENT_HOME/scripts/schedule_task.py" pause <id-or-name>
python "$CHAT_AGENT_HOME/scripts/schedule_task.py" resume <id-or-name>
python "$CHAT_AGENT_HOME/scripts/schedule_task.py" run-now <id-or-name>
python "$CHAT_AGENT_HOME/scripts/schedule_task.py" delete <id-or-name>
```

`run-now` 只表示已提交请求，不要宣称任务已执行成功。删除仅在用户明确要求时执行。

修改名称、内容、时区或调度：

```bash
python "$CHAT_AGENT_HOME/scripts/schedule_task.py" update <id-or-name> --name "新名称"
python "$CHAT_AGENT_HOME/scripts/schedule_task.py" update <id-or-name> --daily "09:00" --weekdays mon-fri
```

## 执行语义

- 任务由独立 `feishu-scheduler` 调度进程持久化执行，不注入当前交互 Codex tmux。
- 任务配置、运行状态和领取记录统一保存在 `state/scheduler.db`；旧版任务 JSON 会自动一次性迁移。
- 每次触发使用独立的 `codex exec --ephemeral`，但以当前对话工作目录为 cwd，因此可读取该对话的 Skill、MCP、memory 和 workspace。
- 最终文字自动发回当前飞书对话；文件和图片可由执行中的 Codex 通过 feishu-send 主动发送。
- 同一任务不重叠执行；执行中点“立即运行”会排队一次后续运行。
- 详情、下次时间、上次结果、次数和最近错误也可在 `/help` →“定时任务”中查看。
