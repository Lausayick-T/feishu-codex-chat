"""
panel.py —— 控制面板专属卡片（/help 打开的那套：主菜单/设置/模型…）。

特点：每张卡底部统一带「← 返回 | ✖️ 关闭」（顶层只留关闭）。
复用 utils/cards.py 的通用原语；按钮 value.action 由 server.on_card_action 路由。
"""

from __future__ import annotations

from pathlib import Path

from . import cards, chatconfig, config, hub, privacy, scheduled, session
from .cards import act, btn, card, div, hr


def _footer(back_action: str | None) -> dict:
    """控制面板统一底部：[← 返回 | ✖️ 关闭]；顶层 back_action=None 时只留关闭。"""
    close = btn("✖️ 关闭", {"action": "close"}, "danger")
    if back_action:
        return act(btn("← 返回", {"action": back_action}), close)
    return act(close)


def _home_footer() -> dict:
    return act(
        btn("🔄 重启当前会话", {"action": "restart"}, "danger"),
        btn("✖️ 关闭", {"action": "close"}, "danger"),
    )


def _resource_update_elements(workdir: Path) -> list[dict]:
    pending = hub.pending_resource_updates(workdir)
    skills = pending["skills"]
    mcp = pending["mcp"]
    if not skills and not mcp:
        return []
    lines = ["**资源有更新**"]
    if skills:
        lines.append(f"• Skill：{'、'.join(skills)}（Codex 会自动检测，通常无需重启）")
    if mcp:
        lines.append(f"• MCP：{'、'.join(mcp)}（配置已同步，当前会话尚未重新载入）")
    elements = [div("\n".join(lines))]
    if mcp:
        elements.append(
            act(btn("🔄 重启当前会话并载入", {"action": "apply_restart", "page": "menu"}, "primary"))
        )
    else:
        elements.append(act(btn("知道了", {"action": "dismiss_skill_updates"}, "primary")))
    elements.append(hr())
    return elements


def closed_card(workdir: Path, changes: list[str] | None = None) -> dict:
    lines = ["✅ 已关闭控制面板"]
    if changes:
        lines.append("")
        lines.append("**本次修改：**")
        lines.extend(f"• {item}" for item in changes)
    lines.append("")
    lines.append("需要时发送 `/help` 重新打开。")
    return card(None, [div("\n".join(lines))])


def main_menu_card(workdir: Path, chat_id: str = "") -> dict:
    cid = chat_id or workdir.name
    if len(cid) > 22:
        cid = cid[:22] + "…"
    cfg = chatconfig.get(workdir)
    return card("🤖 控制面板", [
        *_resource_update_elements(workdir),
        div(f"会话：`{cid}`"),
        div(f"模型：**{chatconfig.engine_model_label(cfg)}**"),
        hr(),
        act(btn("⚙️ 设置", {"action": "settings"}), btn("🧠 模型", {"action": "page_model"})),
        act(btn("🩺 运行状态", {"action": "page_status"}), btn("🔐 隐私与数据", {"action": "page_privacy"})),
        act(btn("📊 用量情况", {"action": "codex_usage"}), btn("⏰ 定时任务", {"action": "page_schedule"})),
        _home_footer(),
    ])


def welcome_card(workdir: Path, chat_id: str = "") -> dict:
    cfg = chatconfig.get(workdir)
    memory = chatconfig.MODE_LABEL.get(cfg.get("memory_mode", "fresh"), "摘要记忆")
    return card("👋 Feishu Codex Chat 已就绪", [
        div(
            "你可以直接发送任务、图片或文件。私聊始终回复；群里只有一位真人时直接回复，"
            "多人群请先 @机器人。\n\n"
            f"当前模型：**{chatconfig.engine_model_label(cfg)}**\n"
            f"当前记忆：**{memory}**（仅保存在运行机器，可随时查看、关闭或清理）"
        ),
        hr(),
        div("快捷命令：`/help` 控制面板　`/status` 运行状态　`/privacy` 隐私与数据"),
        act(
            btn("🚀 打开控制面板", {"action": "menu"}, "primary"),
            btn("🔐 查看隐私数据", {"action": "page_privacy"}),
        ),
        act(btn("🩺 检查运行状态", {"action": "page_status"})),
    ])


def status_card(workdir: Path, chat_id: str) -> dict:
    codex_online = session.exists(session.session_name(chat_id))
    scheduler_online = scheduled.scheduler_health()["online"]
    cfg = chatconfig.get(workdir)
    pending = hub.pending_resource_updates(workdir)
    pending_count = len(pending["skills"]) + len(pending["mcp"])
    lines = [
        "✅ 飞书消息与卡片链路：正常",
        f"{'✅' if codex_online else '⚪'} Codex 会话：{'运行中' if codex_online else '尚未启动；发送任务时会自动启动'}",
        f"{'✅' if scheduler_online else '⚠️'} 定时任务服务：{'运行中' if scheduler_online else '未连接'}",
        f"{'✅' if pending_count == 0 else '⚠️'} 资源配置：{'已同步' if pending_count == 0 else f'{pending_count} 项等待载入'}",
        f"🧠 模型：{chatconfig.engine_model_label(cfg)}",
    ]
    return card("🩺 运行状态", [
        div("\n".join(lines)),
        div("主机启动条件与飞书凭证请在服务器运行 `./start.sh doctor` 检查。"),
        act(btn("🔄 刷新", {"action": "page_status"}, "primary")),
        _footer("menu"),
    ])


def usage_card(workdir: Path, lines: list[str] | None = None, error: str = "") -> dict:
    body = []
    if error:
        body.append(div(f"获取失败：{error}"))
    elif lines:
        for line in lines:
            body.append(div(line))
    else:
        body.append(div("尚未获取。"))
    body.append(act(btn("刷新用量", {"action": "codex_usage"}, "primary")))
    body.append(_footer("menu"))
    return card("📊 用量情况", body)


_TASK_STATUS = {
    "enabled": "✅ 运行中",
    "paused": "⏸️ 已暂停",
    "completed": "🏁 已完成",
}
_RUN_STATUS = {
    "never": "尚未执行",
    "running": "正在执行",
    "success": "成功",
    "failed": "失败",
    "interrupted": "已中断",
}


def schedule_card(workdir: Path, selected: str | None = None) -> dict:
    tasks = scheduled.list_tasks(workdir)
    service = "🟢 调度服务运行中" if scheduled.scheduler_health()["online"] else "🔴 调度服务未连接"
    if not tasks:
        return card("⏰ 定时任务", [
            div(f"{service}\n\n当前对话还没有定时任务。\n\n可直接对 Codex 说：\n「每个工作日早上 8:30，汇总 AI 新闻并发到当前群」"),
            act(btn("🔄 刷新", {"action": "page_schedule"})),
            _footer("menu"),
        ])
    task = next((item for item in tasks if item["id"] == selected), tasks[0])
    options = [
        {
            "label": f"{'✅' if item['status'] == 'enabled' else '⏸️' if item['status'] == 'paused' else '🏁'} {item['name']}",
            "value": item["id"],
        }
        for item in tasks
    ]
    prompt = str(task.get("prompt") or "").strip()
    if len(prompt) > 700:
        prompt = prompt[:697].rstrip() + "..."
    last_error = str(task.get("last_error") or "").strip()
    detail = (
        f"**状态：** {_TASK_STATUS.get(task.get('status'), task.get('status'))}\n"
        f"**计划：** {scheduled.schedule_text(task)}\n"
        f"**下次：** {scheduled.local_time_text(task, task.get('next_run_at'))}\n"
        f"**上次：** {scheduled.local_time_text(task, task.get('last_run_at'))} · "
        f"{_RUN_STATUS.get(task.get('last_status'), task.get('last_status'))}\n"
        f"**次数：** {int(task.get('run_count') or 0)}\n"
        f"**ID：** `{task['id']}`"
    )
    if last_error:
        detail += f"\n**最近错误：** {config.redact_text(last_error)[:500]}"
    toggle = (
        btn("▶️ 恢复", {"action": "schedule_resume", "task_id": task["id"]}, "primary")
        if task["status"] == "paused"
        else btn("⏸️ 暂停", {"action": "schedule_pause", "task_id": task["id"]})
    )
    return card("⏰ 定时任务", [
        div(service),
        cards.select("选择定时任务", options, task["id"], {"action": "sel_schedule"}),
        div(detail),
        hr(),
        div(f"**任务内容**\n{prompt}"),
        act(btn("▶️ 立即运行", {"action": "schedule_run", "task_id": task["id"]}, "primary"), toggle),
        act(btn("🔄 刷新", {"action": "page_schedule"}), btn("🗑 删除", {"action": "schedule_delete_ask", "task_id": task["id"]}, "danger")),
        _footer("menu"),
    ])


def confirm_schedule_delete_card(task: dict) -> dict:
    return card("⚠️ 确认删除定时任务", [
        div(
            f"确认删除「**{task['name']}**」？\n"
            f"计划：{scheduled.schedule_text(task)}\n\n"
            "任务定义和历史运行记录都会被删除。"
        ),
        act(
            btn("🗑 确认删除", {"action": "schedule_delete", "task_id": task["id"]}, "danger"),
            btn("← 取消", {"action": "page_schedule"}),
        ),
    ])


def settings_menu_card(workdir: Path) -> dict:
    entries = [
        btn("🧠 记忆模式", {"action": "page_memory"}),
        btn("💬 群聊回复", {"action": "page_reply"}),
        btn("🧩 Skill", {"action": "page_skill"}),
        btn("🔌 MCP", {"action": "page_mcp"}),
        btn("🔐 隐私与数据", {"action": "page_privacy"}),
    ]
    elements: list = []
    for i in range(0, len(entries), 2):
        pair = entries[i : i + 2]
        elements.append(act(*pair, half=(len(pair) == 1)))
    elements.append(_footer("menu"))
    return card("⚙️ 设置", elements)


def _retention_text(days: int) -> str:
    return "不自动清理" if days == 0 else f"保留 {days} 天"


def privacy_card(workdir: Path) -> dict:
    cfg = chatconfig.get(workdir)
    data = privacy.inventory(workdir)
    labels = {
        "memory": "摘要记忆",
        "attachments": "收到的附件",
        "workspace": "生成的文件",
        "task_logs": "定时任务日志",
    }
    lines = ["所有数据仅保存在运行机器的当前会话目录，不会进入公开 Git 仓库。"]
    for name in ("memory", "attachments", "workspace", "task_logs"):
        item = data[name]
        extra = f"，{item.get('records', 0)} 条数据库记录" if name == "task_logs" else ""
        lines.append(f"• {labels[name]}：{item['files']} 个文件，{privacy.format_bytes(item['bytes'])}{extra}")
    options = [
        {"label": _retention_text(days), "value": str(days)}
        for days in privacy.RETENTION_OPTIONS
    ]
    return card("🔐 隐私与数据", [
        div("\n".join(lines)),
        hr(),
        cards.select(
            "附件自动清理",
            options,
            str(int(cfg.get("attachment_retention_days", 30) or 0)),
            {"action": "set_retention", "kind": "attachments"},
        ),
        cards.select(
            "定时日志自动清理",
            options,
            str(int(cfg.get("task_log_retention_days", 30) or 0)),
            {"action": "set_retention", "kind": "task_logs"},
        ),
        hr(),
        act(
            btn("🧹 清理附件", {"action": "privacy_clear_ask", "category": "attachments"}),
            btn("🧹 清理任务日志", {"action": "privacy_clear_ask", "category": "task_logs"}),
        ),
        act(
            btn("🗑 清空摘要记忆", {"action": "privacy_clear_ask", "category": "memory"}, "danger"),
            btn("🗑 清空生成文件", {"action": "privacy_clear_ask", "category": "workspace"}, "danger"),
        ),
        act(btn("🆕 重置临时上下文", {"action": "privacy_clear_ask", "category": "context"}, "danger")),
        _footer("settings_menu"),
    ])


def confirm_privacy_clear_card(category: str) -> dict:
    labels = {
        "attachments": ("收到的附件", "已下载到当前会话的附件副本"),
        "task_logs": ("定时任务日志", "运行输出与历史记录；任务定义会保留"),
        "memory": ("摘要记忆", "长期记忆和滚动摘要"),
        "workspace": ("生成文件", "当前会话 workspace 中的全部产物"),
        "context": ("临时对话上下文", "当前 Codex 会话；本地摘要记忆会保留"),
    }
    title, detail = labels.get(category, ("本地数据", "所选数据"))
    return card(f"⚠️ 确认清理{title}", [
        div(f"即将删除：{detail}。\n\n此操作不可恢复，且只影响当前飞书会话。"),
        act(
            btn("确认清理", {"action": "privacy_clear", "category": category}, "danger"),
            btn("← 取消", {"action": "page_privacy"}),
        ),
    ])


def memory_card(workdir: Path) -> dict:
    mode = chatconfig.get(workdir).get("memory_mode", "resume")
    return card("🧠 记忆模式", [
        div(f"**当前：** {chatconfig.MODE_LABEL.get(mode, mode)}\n\n"
            "• 当前会话上下文：依赖常驻 Codex TUI；重启后不保证恢复完整聊天。\n"
            "• 摘要记忆：维护 `memory/MEMORY.md` 和 `memory/history_chat.md`，用于跨重启恢复关键信息。"),
        act(
            btn("当前会话上下文", {"action": "set_memory", "mode": "resume"}, "primary" if mode == "resume" else "default"),
            btn("摘要记忆", {"action": "set_memory", "mode": "fresh"}, "primary" if mode == "fresh" else "default"),
        ),
        _footer("settings_menu"),
    ])


def reply_card(workdir: Path) -> dict:
    return card("💬 群聊回复策略", [
        div("**当前：自动判断**\n\n"
            "• 群里只有 1 位真人：回复所有消息，无需 @机器人。\n"
            "• 群里有 2 位及以上真人：仅回复 @机器人的消息。\n"
            "• 私聊：始终回复。\n\n"
            "成员数量会短时缓存；无法读取群成员时按“仅 @”处理。"),
        _footer("settings_menu"),
    ])


def _status_text(on: bool) -> str:
    return "✅ 使用中" if on else "🚫 已禁用"


def _status_options(items: list[str], state_fn=None, *, system: bool = False) -> list[dict]:
    opts = []
    for name in items:
        if system:
            label = f"✅ {name}（系统启用）"
        elif state_fn:
            label = f"{'✅' if state_fn(name) else '🚫'} {name}"
        else:
            label = name
        opts.append({"label": label, "value": name})
    return opts


def _section(title: str, items: list[str], selected: str | None, sel_action: str,
             desc: str, button_rows: list[list[dict]], *, options: list[dict] | None = None,
             status: str = "") -> list[dict]:
    """一个分区：标题 + 下拉(选具体项) + 说明文本 + 若干行按钮。"""
    els = [hr(), div(f"**{title}**")]
    if not items:
        els.append(div("_（无）_"))
        return els
    sel = selected if selected in items else items[0]
    els.append(cards.select(f"选择{title}", options or items, sel, {"action": sel_action}))
    if status:
        els.append(div(f"状态：**{status}**"))
    els.append(div(desc or "（无说明）"))
    for row in button_rows:
        if row:
            els.append(act(*row))
    return els


def _use_disable(on: bool, base_action: str, name: str, extra: list[dict] | None = None) -> list[dict]:
    btns = [
        btn("✅ 使用", {"action": f"{base_action}_use", "name": name}, "primary" if not on else "default"),
        btn("🚫 禁用", {"action": f"{base_action}_disable", "name": name}, "primary" if on else "default"),
    ]
    return btns + (extra or [])


def skill_card(workdir: Path, sel: dict | None = None) -> dict:
    sel = sel or {}
    sysl = hub.system_skills()
    spec = hub.special_skills(workdir)
    genl = hub.general_skills()
    sys_sel = sel.get("sys") if sel.get("sys") in sysl else (sysl[0] if sysl else None)
    spec_sel = sel.get("special") if sel.get("special") in spec else (spec[0] if spec else None)
    gen_sel = sel.get("general") if sel.get("general") in genl else (genl[0] if genl else None)

    els = [div("按分区选择 Skill。新增和修改会被 Codex 自动检测，通常无需重启。")]
    els += _section("系统（默认·不可禁用）", sysl, sys_sel, "sel_skill_sys",
                    hub.skill_desc(workdir, sys_sel) if sys_sel else "", [],
                    options=_status_options(sysl, system=True),
                    status="✅ 系统默认，始终启用" if sys_sel else "")
    spec_state = lambda name: hub.special_skill_enabled(workdir, name)
    spec_rows = ([
        _use_disable(spec_state(spec_sel), "skill_spec", spec_sel),
        [btn("📤 升级为通用", {"action": "skill_publish", "name": spec_sel}),
         btn("🗑 删除", {"action": "skill_spec_delete_ask", "name": spec_sel}, "danger")],
    ] if spec_sel else [])
    els += _section("专用（本群独有）", spec, spec_sel, "sel_skill_special",
                    hub.skill_desc(workdir, spec_sel) if spec_sel else "", spec_rows,
                    options=_status_options(spec, spec_state),
                    status=_status_text(spec_state(spec_sel)) if spec_sel else "")
    gen_state = lambda name: hub.skill_loaded(workdir, name)
    gen_rows = ([_use_disable(gen_state(gen_sel), "skill_gen", gen_sel)] if gen_sel else [])
    els += _section("通用（hub 可选）", genl, gen_sel, "sel_skill_general",
                    hub.skill_desc(workdir, gen_sel) if gen_sel else "", gen_rows,
                    options=_status_options(genl, gen_state),
                    status=_status_text(gen_state(gen_sel)) if gen_sel else "")
    els.append(_footer("settings_menu"))
    return card("🧩 Skill", els)


def mcp_card(workdir: Path, sel: dict | None = None) -> dict:
    sel = sel or {}
    sysl = hub.system_mcp()
    spec = hub.special_mcp(workdir)
    genl = hub.general_mcp()
    sys_sel = sel.get("sys") if sel.get("sys") in sysl else (sysl[0] if sysl else None)
    spec_sel = sel.get("special") if sel.get("special") in spec else (spec[0] if spec else None)
    gen_sel = sel.get("general") if sel.get("general") in genl else (genl[0] if genl else None)

    if not sysl and not spec and not genl:
        return card("🔌 MCP", [
            div(
                "当前没有 MCP。公开版本默认不附带个人或第三方 MCP 配置。\n\n"
                "需要时可直接对 Codex 说：\n"
                "「为当前会话创建一个 MCP，并先告诉我它需要哪些权限和环境变量。」\n\n"
                "配置只会保存在本机当前会话，升级为通用资源前会执行隐私校验。"
            ),
            _footer("settings_menu"),
        ])

    els = [div("按分区选择 MCP。改动只会更新配置，不会自动重启其他或当前会话。")]
    els += _section("系统（默认·不可禁用）", sysl, sys_sel, "sel_mcp_sys",
                    hub.mcp_desc(workdir, sys_sel) if sys_sel else "", [],
                    options=_status_options(sysl, system=True),
                    status="✅ 系统默认，始终启用" if sys_sel else "")
    spec_state = lambda name: hub.special_mcp_enabled(workdir, name)
    spec_rows = ([
        _use_disable(spec_state(spec_sel), "mcp_spec", spec_sel),
        [btn("📤 升级为通用", {"action": "mcp_publish", "name": spec_sel}),
         btn("🗑 删除", {"action": "mcp_spec_delete_ask", "name": spec_sel}, "danger")],
    ] if spec_sel else [])
    els += _section("专用（本群独有）", spec, spec_sel, "sel_mcp_special",
                    hub.mcp_desc(workdir, spec_sel) if spec_sel else "", spec_rows,
                    options=_status_options(spec, spec_state),
                    status=_status_text(spec_state(spec_sel)) if spec_sel else "")
    gen_state = lambda name: hub.mcp_loaded(workdir, name)
    gen_rows = ([_use_disable(gen_state(gen_sel), "mcp_gen", gen_sel)] if gen_sel else [])
    els += _section("通用（hub 可选）", genl, gen_sel, "sel_mcp_general",
                    hub.mcp_desc(workdir, gen_sel) if gen_sel else "", gen_rows,
                    options=_status_options(genl, gen_state),
                    status=_status_text(gen_state(gen_sel)) if gen_sel else "")
    els.append(hr())
    els.append(act(btn("🔄 重启当前会话并载入", {"action": "apply_restart", "page": "mcp"}, "primary")))
    els.append(_footer("settings_menu"))
    return card("🔌 MCP", els)


def confirm_delete_card(kind: str, name: str) -> dict:
    """删除专用项的二次确认。kind = skill / mcp。"""
    label = "Skill" if kind == "skill" else "MCP"
    back = "page_skill" if kind == "skill" else "page_mcp"
    return card("⚠️ 确认删除", [
        div(f"确认删除本群专用 {label}「**{name}**」？\n此操作**不可恢复**。"),
        act(
            btn("🗑 确认删除", {"action": f"{kind}_spec_delete", "name": name}, "danger"),
            btn("← 取消", {"action": back}),
        ),
    ])


def model_card(workdir: Path, draft: dict | None = None) -> dict:
    cfg = chatconfig.get(workdir)
    configured_model = cfg.get("codex_model", chatconfig.DEFAULTS["codex_model"])
    configured_effort = cfg.get("codex_effort", chatconfig.DEFAULTS["codex_effort"])
    draft = draft or {}
    cur = draft.get("model", configured_model)
    effort = chatconfig.normalize_codex_effort(cur, draft.get("effort", configured_effort))
    effective = cfg.get("codex_effective_model", "")
    items = list(chatconfig.CODEX_MODELS.items())
    model_rows = []
    for i in range(0, len(items), 2):
        pair = [
            btn(label, {"action": "stage_codex_model", "model": model}, "primary" if cur == model else "default")
            for model, label in items[i : i + 2]
        ]
        model_rows.append(act(*pair, half=(len(pair) == 1)))
    effort_items = [
        (item, chatconfig.CODEX_EFFORT_LABELS[item])
        for item in chatconfig.codex_efforts_for(cur)
    ]
    effort_rows = []
    for i in range(0, len(effort_items), 2):
        pair = [
            btn(label, {"action": "stage_codex_effort", "effort": item}, "primary" if effort == item else "default")
            for item, label in effort_items[i : i + 2]
        ]
        effort_rows.append(act(*pair, half=(len(pair) == 1)))
    configured = (
        f"{chatconfig.CODEX_MODELS.get(configured_model, configured_model)} · "
        f"{chatconfig.CODEX_EFFORT_LABELS.get(configured_effort, configured_effort)}"
    )
    pending = (
        f"{chatconfig.CODEX_MODELS.get(cur, cur)} · "
        f"{chatconfig.CODEX_EFFORT_LABELS.get(effort, effort)}"
    )
    changed = cur != configured_model or effort != configured_effort
    return card("🧠 模型", [
        div("先选择模型和 Effort，最后点击一次「应用并重启」；选择过程不会重启。"),
        div(f"当前配置：**{configured}**" +
            (f"\n待应用：**{pending}**" if changed else "") +
            (f"\n实际模型：**{chatconfig.CODEX_MODELS.get(effective, effective)}**" if effective and effective != configured_model else "")),
        hr(),
        div("**模型**"),
        *model_rows,
        hr(),
        div("**Effort**"),
        *effort_rows,
        hr(),
        act(
            btn("✅ 应用并重启（一次）", {"action": "apply_codex_settings"}, "primary"),
            btn("↩️ 取消更改", {"action": "reset_codex_draft"}),
        ),
        _footer("menu"),
    ])


def codex_model_card(workdir: Path, draft: dict | None = None) -> dict:
    """兼容旧卡片导航；当前模型页只展示 Codex 模型。"""
    return model_card(workdir, draft)
