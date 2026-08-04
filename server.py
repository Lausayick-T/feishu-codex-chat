"""
server.py —— 飞书长连接入口 + 群路由 + 事件编排（瘦身后只做编排）。

分层：
  utils/feishu.py     飞书 API
  utils/session.py    tmux Codex 会话
  utils/chatconfig.py per-chat 配置 + 切换逻辑
  utils/cards.py      通用卡片原语（btn/act/card + 思考/回复卡，可复用）
  utils/panel.py      控制面板专属卡片（带 返回/关闭 footer）
  utils/workers.py    进度卡 / codex 回复后台线程
  bots/<群>/          每个任务的工作区

运行： uv run python -u server.py
"""

from __future__ import annotations

import json
import copy
import fcntl
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path


_FEISHU_WS_HOST = "msg-frontier.feishu.cn"


def _ensure_feishu_ws_no_proxy() -> None:
    """Keep Feishu WebSocket off HTTP proxies that break upgrade/keepalive handshakes."""
    for key in ("NO_PROXY", "no_proxy"):
        hosts = [item.strip() for item in os.environ.get(key, "").split(",") if item.strip()]
        if _FEISHU_WS_HOST not in hosts and "*" not in hosts:
            hosts.append(_FEISHU_WS_HOST)
        os.environ[key] = ",".join(hosts)


_ensure_feishu_ws_no_proxy()

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger,
    P2CardActionTriggerResponse,
)

from utils import bitable, cards, chatconfig, config, feishu, hub, panel, privacy, questions, scheduled, session, usage, workers

HERE = Path(__file__).resolve().parent
CONFIG = config.load()
config.install_log_redaction()
BOTS = HERE / "bots"
REGISTRY_PATH = HERE / "registry.json"
LOCK_PATH = HERE / "state" / "server.lock"
_LOCK_FILE = None

_seen_msgs: set[str] = set()  # message_id 去重（飞书可能重投）
_pending_lock = threading.Lock()
_pending_timers: dict[str, threading.Timer] = {}
_PENDING_ATTACHMENT_NOTICE_SEC = 300


def _acquire_single_instance() -> None:
    """避免多个飞书长连接实例同时处理同一条消息。"""
    global _LOCK_FILE
    LOCK_PATH.parent.mkdir(exist_ok=True)
    _LOCK_FILE = LOCK_PATH.open("w", encoding="utf-8")
    try:
        fcntl.flock(_LOCK_FILE, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("chat-agent 已有一个 server 实例在运行，本次启动退出。")
        sys.exit(0)
    _LOCK_FILE.seek(0)
    _LOCK_FILE.truncate()
    _LOCK_FILE.write(str(os.getpid()))
    _LOCK_FILE.flush()


# ---------- 路由 ----------
def load_registry() -> dict:
    if not REGISTRY_PATH.exists():
        return {"chats": {}}
    try:
        value = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"registry.json 不是有效 JSON：{exc}") from exc
    return value if isinstance(value, dict) else {"chats": {}}


def save_registry(reg: dict) -> None:
    REGISTRY_PATH.write_text(json.dumps(reg, ensure_ascii=False, indent=2), encoding="utf-8")


def workdir_for(chat_id: str) -> Path | None:
    """按 chat_id 解析工作目录；未注册且开启 auto_create 则按模板新建并登记。"""
    reg = load_registry()
    name = reg.get("chats", {}).get(chat_id)
    if name:
        target = BOTS / name
        privacy.cleanup_if_due(target)
        return target
    if not CONFIG.get("routing", {}).get("auto_create", True):
        return None
    name = f"auto_{session.safe_chat_id(chat_id)}"
    target = BOTS / name
    if not target.exists():
        shutil.copytree(BOTS / "_template", target)
        chatconfig.init_defaults(target)
    reg.setdefault("chats", {})[chat_id] = name
    save_registry(reg)
    privacy.cleanup_if_due(target)
    print(f"[route] 新群 {chat_id} → bots/{name}")
    return target


def _existing_workdir(chat_id: str) -> Path | None:
    """只解析已注册的群（不自动创建），给 bitable 同步用。"""
    name = load_registry().get("chats", {}).get(chat_id)
    return (BOTS / name) if name else None


def apply_from_bitable() -> str:
    """读多维表格并对齐各群资源；Skill 动态更新，MCP 仅标记待用户载入。"""
    if not bitable.enabled():
        return "未配置多维表格"
    d = hub.defaults()
    changed = []
    for chat_id, want_sk, want_mcp, over_str in bitable.read_all_desired():
        wd = _existing_workdir(chat_id)
        if wd is None:
            continue
        # MCP 参数覆盖：解析表里的 JSON，变了就存进 agent.json（坏 JSON 则忽略）
        over_changed = False
        try:
            new_over = json.loads(over_str) if over_str.strip() else {}
        except json.JSONDecodeError:
            new_over = None
        if isinstance(new_over, dict) and new_over != chatconfig.get(wd).get("mcp_overrides", {}):
            chatconfig.set_value(wd, "mcp_overrides", new_over)
            over_changed = True

        want_sk = set(want_sk) | set(d["skills"])
        want_mcp = set(want_mcp) | set(d["mcp"])
        resource_changes: list[str] = []
        for n in hub.list_skills():
            if n in want_sk and not hub.skill_loaded(wd, n):
                hub.load_skill(wd, n); resource_changes.append(f"skill:{n}")
            elif n not in want_sk and hub.skill_loaded(wd, n):
                hub.unload_skill(wd, n); resource_changes.append(f"skill:{n}:removed")
        for n in hub.list_mcp():
            want = n in want_mcp
            # 覆盖变了 → 更新配置；当前会话仍由用户在控制面板确认载入。
            if want and (not hub.mcp_loaded(wd, n) or over_changed):
                hub.load_mcp(wd, n); resource_changes.append(f"mcp:{n}")
            elif not want and hub.mcp_loaded(wd, n):
                hub.unload_mcp(wd, n); resource_changes.append(f"mcp:{n}:removed")
        if resource_changes:
            if session.exists(session.session_name(chat_id)):
                hub.mark_resource_updates(wd, resource_changes)
            changed.append(chat_id)
    return f"✅ 已按表格同步 {len(changed)} 个群；未自动重启任何会话"


def reconcile_bitable() -> str:
    """双向对账：表→文件，再由文件→表回写状态并刷新目录表。"""
    msg = apply_from_bitable()
    try:
        for chat_id, name in load_registry().get("chats", {}).items():
            bitable.sync_group(chat_id, BOTS / name)
        bitable.ensure_catalog()
    except Exception as e:
        print(f"[bitable] 回写失败: {e}")
    return msg


def _walk(value):
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from _walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk(v)


def extract_text(message) -> str:
    """从消息体取纯文本，去掉 @机器人 占位符。支持 text 和富文本 post。"""
    try:
        content = json.loads(message.content)
    except Exception:
        return ""
    if message.message_type == "text":
        text = content.get("text", "")
    elif message.message_type == "post":
        parts = []
        for item in _walk(content.get("content", content)):
            if item.get("tag") == "text" and item.get("text"):
                parts.append(str(item.get("text")))
        text = "\n".join(p.strip() for p in parts if p.strip())
    else:
        text = ""
    return re.sub(r"@_user_\d+", "", text).strip()


@dataclass
class Attachment:
    kind: str
    path: Path
    name: str


def _safe_chat(chat_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", chat_id)


def _pending_key(chat_id: str, sender_id: str) -> str:
    return f"{_safe_chat(chat_id)}__{_safe_chat(sender_id or 'unknown')}"


def _pending_attachment_path(key: str) -> Path:
    return HERE / "state" / f"{key}.pending_attachments.json"


def _attachment_to_dict(a: Attachment) -> dict:
    return {"kind": a.kind, "path": str(a.path), "name": a.name, "created_at": time.time(), "notified": False}


def _dict_to_attachment(d: dict) -> Attachment | None:
    try:
        path = Path(str(d.get("path", "")))
        if not path.exists():
            return None
        return Attachment(str(d.get("kind") or "file"), path, str(d.get("name") or path.name))
    except Exception:
        return None


def _read_pending_attachment_dicts(key: str) -> list[dict]:
    p = _pending_attachment_path(key)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
    except Exception:
        return []


def _write_pending_attachment_dicts(key: str, items: list[dict]) -> None:
    p = _pending_attachment_path(key)
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _cancel_pending_notice(key: str) -> None:
    timer = _pending_timers.pop(key, None)
    if timer:
        timer.cancel()


def _pending_notice(chat_id: str, key: str) -> None:
    with _pending_lock:
        items = _read_pending_attachment_dicts(key)
        if not any(not x.get("notified") for x in items):
            return
        for item in items:
            item["notified"] = True
        _write_pending_attachment_dicts(key, items)
        _pending_timers.pop(key, None)
    try:
        feishu.send_text(chat_id, "文件已经收到，需要我做什么处理呢？")
    except Exception as e:
        print(f"[attach] 文件收到提示发送失败 chat={chat_id}: {e}")


def _schedule_pending_notice(chat_id: str, key: str) -> None:
    _cancel_pending_notice(key)
    timer = threading.Timer(_PENDING_ATTACHMENT_NOTICE_SEC, _pending_notice, args=(chat_id, key))
    timer.daemon = True
    _pending_timers[key] = timer
    timer.start()


def _stage_attachments(chat_id: str, sender_id: str, attachments: list[Attachment], *, schedule_notice: bool) -> None:
    if not attachments:
        return
    key = _pending_key(chat_id, sender_id)
    with _pending_lock:
        items = _read_pending_attachment_dicts(key)
        items.extend(_attachment_to_dict(a) for a in attachments)
        _write_pending_attachment_dicts(key, items)
        if schedule_notice:
            _schedule_pending_notice(chat_id, key)
        else:
            _cancel_pending_notice(key)


def _consume_pending_attachments(chat_id: str, sender_id: str) -> list[Attachment]:
    key = _pending_key(chat_id, sender_id)
    with _pending_lock:
        items = _read_pending_attachment_dicts(key)
        _pending_attachment_path(key).unlink(missing_ok=True)
        _cancel_pending_notice(key)
    return [a for item in items if (a := _dict_to_attachment(item))]


def _sender_id(data: P2ImMessageReceiveV1) -> str:
    sender = getattr(data.event, "sender", None)
    sid = getattr(sender, "sender_id", None)
    return (
        getattr(sid, "open_id", "")
        or getattr(sid, "union_id", "")
        or getattr(sid, "user_id", "")
        or getattr(sender, "sender_type", "")
        or "unknown"
    )


def _safe_filename(name: str, fallback: str) -> str:
    name = name or fallback
    name = re.sub(r"[\\/:*?\"<>|\r\n]+", "_", name).strip(". ")
    return name or fallback


def _message_content(message) -> dict:
    try:
        return json.loads(message.content)
    except Exception:
        return {}


def _download_message_image(message, image_key: str, path: Path) -> None:
    try:
        feishu.download_message_resource(message.message_id, image_key, "image", path)
    except Exception:
        feishu.download_image(image_key, path)


def _download_attachments(message, workdir: Path) -> list[Attachment]:
    content = _message_content(message)
    msg_type = message.message_type
    out_dir = workdir / "incoming" / _safe_filename(message.message_id, "message")
    out: list[Attachment] = []
    try:
        if msg_type == "image":
            image_key = content.get("image_key") or content.get("key")
            if image_key:
                path = out_dir / f"{_safe_filename(image_key, 'image')}.jpg"
                _download_message_image(message, image_key, path)
                out.append(Attachment("image", path, path.name))
        elif msg_type == "post":
            seen: set[str] = set()
            for item in _walk(content.get("content", content)):
                if item.get("tag") not in {"img", "image"}:
                    continue
                image_key = item.get("image_key") or item.get("key")
                if not image_key or image_key in seen:
                    continue
                seen.add(image_key)
                path = out_dir / f"{_safe_filename(image_key, 'image')}.jpg"
                _download_message_image(message, image_key, path)
                out.append(Attachment("image", path, path.name))
        elif msg_type in {"file", "media", "audio"}:
            file_key = content.get("file_key") or content.get("key")
            name = _safe_filename(content.get("file_name") or content.get("name") or file_key or "attachment", "attachment")
            if file_key:
                path = out_dir / name
                resource_type = "file" if msg_type == "file" else msg_type
                feishu.download_message_resource(message.message_id, file_key, resource_type, path)
                out.append(Attachment(msg_type, path, name))
    except Exception as e:
        print(f"[attach] 下载失败 type={msg_type} msg={message.message_id}: {e}")
    return out


def _reference_prompt(text: str, attachments: list[Attachment]) -> str:
    if not attachments:
        return text
    paths = "\n".join(str(item.path.resolve()) for item in attachments)
    return f"【附件】\n{paths}\n\n{text.strip()}"


def _send_helper_hint(text: str) -> str:
    if not re.search(r"(发给我|发送给我|传给我|发一下|发送一下|以文件|文件形式|发文件|发图片|上传给我)", text):
        return text
    hint = (
        "\n\n[chat-agent 系统提示]\n"
        "用户可能希望你通过飞书主动发送内容，而不是只在最终回复中粘贴文本。\n"
        "如需发送文件：先创建本地文件，然后运行：\n"
        'python "$CHAT_AGENT_HOME/scripts/feishu_send.py" file path/to/file\n'
        "如需发送图片：\n"
        'python "$CHAT_AGENT_HOME/scripts/feishu_send.py" image path/to/image.png\n'
        "如需发送 Markdown 渲染卡片：\n"
        'python "$CHAT_AGENT_HOME/scripts/feishu_send.py" md-file path/to/report.md\n'
        "发送成功后，最终回复仍应给出有用的总结、关键结论、风险或下一步；不要逐字重复文件全文。\n"
        "如有用户必须第一时间看到的结论或注意事项，可先用 text/md 主动发送，再发送文件。\n"
    )
    return text + hint


def _append_to_active_turn(
    chat_id: str,
    wd: Path,
    text: str,
    attachments: list[Attachment],
    *,
    apply_helper_hint: bool = True,
) -> bool:
    prompt = _reference_prompt(text, attachments)
    if apply_helper_hint:
        prompt = _send_helper_hint(prompt)
    supplement_prompt = f"[用户补充当前任务]\n{prompt}"
    active = workers.append_active_supplement(chat_id, supplement_prompt)
    if active is None:
        return False

    old_message_id = str(active.get("message_id") or "")
    if old_message_id:
        elapsed = max(0, int(time.time() - float(active.get("started_at") or time.time())))
        try:
            if active.get("starting"):
                card = cards.codex_starting_card(elapsed, str(active.get("supplement_note") or ""))
            else:
                card = cards.thinking_card(
                    str(active.get("activity") or "正在处理…"),
                    elapsed,
                    str(active.get("supplement_note") or ""),
                )
            feishu.update_card(old_message_id, card)
        except Exception as e:
            print(f"[msg] 追问旧卡冻结失败: {e}")

    new_message_id = ""
    try:
        activity = str(active.get("activity") or "正在处理…")
        new_card = (
            cards.codex_starting_card(0)
            if active.get("starting")
            else cards.thinking_card(activity, 0)
        )
        response = feishu.send_card(chat_id, new_card)
        new_message_id = str((response.get("data") or {}).get("message_id") or "")
        if new_message_id:
            workers.rotate_active_card(chat_id, new_message_id)
        else:
            workers.cancel_active_handoff(chat_id)
    except Exception as e:
        workers.cancel_active_handoff(chat_id)
        print(f"[msg] 追问新卡创建失败: {e}")

    if active.get("accepting"):
        session.send(chat_id, supplement_prompt)
    print(f"[msg] chat={chat_id} 追加当前任务 text={text[:50]!r} attachments={len(attachments)}")
    return True


def _dispatch_to_agent(
    chat_id: str,
    wd: Path,
    text: str,
    turn_id: str,
    sender_id: str,
    attachments: list[Attachment] | None = None,
    *,
    apply_helper_hint: bool = True,
    clear_fresh: bool = True,
) -> None:
    """Send a user-like turn into the active agent session and start progress delivery."""
    attachments = attachments or []
    if not workers.begin_active_turn(chat_id):
        _append_to_active_turn(
            chat_id,
            wd,
            text,
            attachments,
            apply_helper_hint=apply_helper_hint,
        )
        return

    workers.clear_final(chat_id)
    card_mid = ""
    try:
        r = feishu.send_card(chat_id, cards.thinking_card("收到，准备中…", 0))
        card_mid = r["data"]["message_id"]
        workers.set_active_card(chat_id, card_mid)
    except Exception as e:
        print(f"[msg] 发思考卡失败: {e}")

    progress_started = False

    def _start_progress_once() -> None:
        nonlocal progress_started
        if progress_started:
            return
        progress_started = True
        # 从用户消息进入系统起就维护同一条计时线；冷启动与正式执行不再各自从 0 开始。
        threading.Thread(target=workers.progress_worker, args=(chat_id, card_mid, wd), daemon=True).start()

    def _show_codex_starting() -> None:
        workers.set_active_starting(chat_id, True)
        if card_mid:
            try:
                active = workers.active_turn(chat_id) or {}
                elapsed = max(0, int(time.time() - float(active.get("started_at") or time.time())))
                feishu.update_card(
                    card_mid,
                    cards.codex_starting_card(
                        elapsed,
                        supplement=str(active.get("supplement_note") or ""),
                    ),
                )
            except Exception as e:
                print(f"[msg] Codex 启动卡更新失败: {e}")
        _start_progress_once()

    try:
        cold = session.ensure(
            chat_id,
            wd,
            cold_wait=CONFIG.get("cold_start_wait_sec", 6),
            on_start=_show_codex_starting,
        )
        _start_progress_once()
        codex_baseline = workers.codex_reply_baseline(wd)
        prompt = _reference_prompt(text, attachments)
        if apply_helper_hint:
            prompt = _send_helper_hint(prompt)
        session.send(chat_id, prompt)
        for supplement in workers.activate_turn(chat_id):
            session.send(chat_id, supplement)
    except Exception:
        workers.end_active_turn(chat_id)
        raise
    print(f"[msg] chat={chat_id} sender={sender_id} engine=codex cold={cold} turn={turn_id} text={text[:50]!r} attachments={len(attachments)}")

    threading.Thread(target=workers.codex_reply_worker, args=(chat_id, wd, codex_baseline), daemon=True).start()


def _is_at_bot(message) -> bool:
    try:
        bot_id = feishu.get_bot_open_id()
    except Exception:
        bot_id = ""
    for m in (message.mentions or []):
        if m.id and getattr(m.id, "open_id", "") == bot_id:
            return True
    return False


def _automatic_group_reply_mode(chat_id: str) -> str:
    """单真人群全部回复；多人群或成员数不可用时仅响应 @。"""
    human_count = feishu.chat_human_member_count(chat_id)
    return "all" if human_count == 1 else "at_only"


def _handle_question_text(chat_id: str, text: str) -> bool:
    state = questions.find_awaiting(chat_id)
    if not state:
        return False
    questions.answer_custom(state, text)
    questions.save(state)
    try:
        if state.get("message_id"):
            feishu.update_card(state["message_id"], questions.question_card(state))
        else:
            feishu.send_card(chat_id, questions.question_card(state))
    except Exception as e:
        print(f"[question] 更新自定义答案卡片失败 chat={chat_id}: {e}")
    print(f"[question] custom answer chat={chat_id} question={state.get('id')} text={text[:40]!r}")
    return True


def _control_panel_card(chat_id: str, wd: Path) -> dict:
    """Refresh this chat's resources before showing update/reload guidance."""
    try:
        changes = hub.reconcile_loaded_resources(wd)
        if changes and session.exists(session.session_name(chat_id)):
            hub.mark_resource_updates(wd, changes)
    except Exception as exc:
        print(f"[hub-sync] 控制面板打开前对账失败 chat={chat_id}: {exc}")
    return panel.main_menu_card(wd, chat_id)


# ---------- 收消息 ----------
def on_message(data: P2ImMessageReceiveV1) -> None:
    msg = data.event.message
    chat_id = msg.chat_id
    sender_id = _sender_id(data)
    if msg.message_id in _seen_msgs:
        return
    _seen_msgs.add(msg.message_id)

    supported_types = {"text", "post", "image", "file", "media", "audio"}
    if msg.message_type not in supported_types:
        print(f"[skip] 暂不支持消息 type={msg.message_type}")
        return

    is_new = chat_id not in load_registry().get("chats", {})
    wd = workdir_for(chat_id)
    if wd is None:
        print(f"[skip] 群 {chat_id} 未注册且未开启 auto_create")
        return
    if is_new and msg.message_type in {"text", "post"}:  # 兜底：未收到入群事件时，首条文本消息也欢迎+登记
        try:
            _send_welcome(chat_id, wd)
        except Exception as e:
            print(f"[welcome] 失败: {e}")
        if bitable.enabled():
            threading.Thread(target=lambda: bitable.sync_group(chat_id, wd), daemon=True).start()

    text = extract_text(msg) if msg.message_type in {"text", "post"} else ""
    current_attachments = _download_attachments(msg, wd) if msg.message_type != "text" else []
    group_reply_mode = (
        _automatic_group_reply_mode(chat_id) if msg.chat_type == "group" else "all"
    )

    if msg.message_type not in {"text", "post"} or (msg.message_type == "post" and not text):
        should_notice = group_reply_mode == "all"
        _stage_attachments(chat_id, sender_id, current_attachments, schedule_notice=should_notice)
        print(f"[attach] chat={chat_id} sender={sender_id} type={msg.message_type} saved={len(current_attachments)} notice={should_notice}")
        return

    if not text:
        return

    # / 命令：本地处理，不喂给引擎
    if text.startswith("/"):
        cmd = text.split()[0].lower()
        if cmd in ("/help", "/menu", "/panel", "/setting", "/settings"):
            feishu.send_card(chat_id, _control_panel_card(chat_id, wd))
        elif cmd == "/status":
            feishu.send_card(chat_id, panel.status_card(wd, chat_id))
        elif cmd == "/privacy":
            feishu.send_card(chat_id, panel.privacy_card(wd))
        elif cmd == "/sync":  # 手动双向对账多维表格
            feishu.send_text(chat_id, reconcile_bitable())
        else:
            feishu.send_text(chat_id, "发送 /help 打开控制面板。")
        print(f"[cmd] chat={chat_id} cmd={text[:30]!r}")
        return

    if _handle_question_text(chat_id, text):
        return

    # 自动群聊回复策略：单真人群全部回复；多人群或成员数不可用时仅响应 @。
    # 附件消息已在上面无条件暂存；这里只决定文本是否触发模型处理。
    if msg.chat_type == "group" and group_reply_mode == "at_only":
        if not _is_at_bot(msg):
            if current_attachments:
                _stage_attachments(chat_id, sender_id, current_attachments, schedule_notice=False)
            print(f"[skip] chat={chat_id} 多人群或成员数不可用，消息未@机器人")
            return

    attachments = [*_consume_pending_attachments(chat_id, sender_id), *current_attachments]
    if _append_to_active_turn(chat_id, wd, text, attachments):
        return
    _dispatch_to_agent(chat_id, wd, text, msg.message_id, sender_id, attachments)


# ---------- 卡片回调 ----------
def _card_resp(toast: str, card: dict, notice: str = "") -> P2CardActionTriggerResponse:
    if notice:
        card = copy.deepcopy(card)
        card.setdefault("elements", [])
        card["elements"] = [cards.div(f"**本次修改：** {notice}"), cards.hr(), *card["elements"]]
    resp = {"card": {"type": "raw", "data": card}}
    if toast:
        resp["toast"] = {"type": "info", "content": toast}
    return P2CardActionTriggerResponse(resp)


def _restart_and_notify(chat_id: str, wd: Path, reason: str = "") -> None:
    try:
        ready = session.restart(chat_id, wd)
        prefix = f"{reason}：" if reason else ""
        if ready:
            hub.clear_resource_updates(wd)
            mcp_failed = session.codex_startup_warning(chat_id)
            if mcp_failed:
                feishu.send_text(
                    chat_id,
                    f"⚠️ {prefix}当前 Codex 会话已重启并可继续对话；但部分 MCP 加载失败：{mcp_failed}。",
                )
            else:
                feishu.send_text(chat_id, f"✅ {prefix}当前 Codex 会话已重启，可以继续对话。")
        else:
            feishu.send_text(chat_id, f"⚠️ {prefix}已执行重启，但没有确认 Codex 完全就绪；如无法继续对话，请再点一次重启。")
    except Exception as e:
        print(f"[card] 重启失败: {e}")
        try:
            feishu.send_text(chat_id, f"❌ 重启失败：{e}")
        except Exception:
            pass


_HUB_RECONCILE_LOCK = threading.Lock()


def reconcile_hub_resources_once() -> dict:
    """同步 Hub 新版本；只记录控制面板提示，绝不自动重启任何会话。"""
    if not _HUB_RECONCILE_LOCK.acquire(blocking=False):
        return {"updated": 0, "pending": 0}
    result = {"updated": 0, "pending": 0}
    try:
        for chat_id, name in load_registry().get("chats", {}).items():
            wd = BOTS / name
            (wd / hub.LEGACY_RESTART_REQUEST).unlink(missing_ok=True)
            try:
                changes = hub.reconcile_loaded_resources(wd)
            except Exception as exc:
                print(f"[hub-sync] 对账失败 chat={chat_id}: {exc}")
                continue
            running = session.exists(session.session_name(chat_id))
            if changes:
                result["updated"] += 1
                detail = "、".join(changes[:6])
                print(f"[hub-sync] chat={chat_id} updated={detail}")
                if running:
                    hub.mark_resource_updates(wd, changes)
                    result["pending"] += 1
    finally:
        _HUB_RECONCILE_LOCK.release()
    return result


def _hub_reconcile_loop() -> None:
    interval = max(5, int(CONFIG.get("hub_reconcile_interval_sec", 30)))
    time.sleep(min(5, interval))
    while True:
        try:
            reconcile_hub_resources_once()
        except Exception as exc:
            print(f"[hub-sync] 后台对账异常: {exc}")
        time.sleep(interval)


def _apply_codex_settings(chat_id: str, wd: Path, model: str, effort: str) -> str:
    """一次性应用 Codex 模型与 effort，并按需只重启一次。"""
    effort = chatconfig.normalize_codex_effort(model, effort)
    chatconfig.set_values(wd, {
        "engine": "codex",
        "codex_model": model,
        "codex_effort": effort,
        "codex_effective_model": "",
    })
    label = chatconfig.engine_model_label(chatconfig.get(wd))
    if session.exists(session.session_name(chat_id)):
        threading.Thread(target=_restart_and_notify, args=(chat_id, wd, "模型与 Effort 切换"), daemon=True).start()
        return f"已应用：{label}（仅重启一次，记忆保留）"
    return f"已应用：{label}"


# 每个 chat 的卡片下拉选择（内存态，重启归零）：_ui[chat][kind][section]=name
_ui: dict = {}


def _sel(chat_id: str, kind: str) -> dict:
    return _ui.setdefault(chat_id, {}).setdefault(kind, {})


def _model_draft(chat_id: str, wd: Path, *, reset: bool = False) -> dict:
    state = _ui.setdefault(chat_id, {})
    if reset:
        state.pop("model_draft", None)
    cfg = chatconfig.get(wd)
    draft = state.setdefault("model_draft", {
        "model": cfg.get("codex_model", chatconfig.DEFAULTS["codex_model"]),
        "effort": cfg.get("codex_effort", chatconfig.DEFAULTS["codex_effort"]),
    })
    if draft.get("model") not in chatconfig.CODEX_MODELS:
        draft["model"] = cfg.get("codex_model", chatconfig.DEFAULTS["codex_model"])
    draft["effort"] = chatconfig.normalize_codex_effort(
        draft["model"], draft.get("effort", chatconfig.DEFAULTS["codex_effort"])
    )
    return draft


def _record_change(chat_id: str, text: str) -> None:
    if not text:
        return
    changes = _ui.setdefault(chat_id, {}).setdefault("changes", [])
    if not changes or changes[-1] != text:
        changes.append(text)


def _consume_changes(chat_id: str) -> list[str]:
    changes = list(_ui.setdefault(chat_id, {}).get("changes", []))
    _ui.setdefault(chat_id, {})["changes"] = []
    return changes


def _changed_for(chat_id: str, toast: str, card: dict) -> P2CardActionTriggerResponse:
    _record_change(chat_id, toast)
    return _card_resp(toast, card, notice=toast)


# 导航/设置类动作 → 原地刷新控制面板卡片
_NAV = {
    "menu": lambda wd, cid, v: _control_panel_card(cid, wd),
    "page_model": lambda wd, cid, v: panel.model_card(wd, _model_draft(cid, wd)),
    "page_codex_model": lambda wd, cid, v: panel.codex_model_card(wd, _model_draft(cid, wd)),
    "settings": lambda wd, cid, v: panel.settings_menu_card(wd),
    "settings_menu": lambda wd, cid, v: panel.settings_menu_card(wd),
    "page_memory": lambda wd, cid, v: panel.memory_card(wd),
    "page_reply": lambda wd, cid, v: panel.reply_card(wd),
    "page_privacy": lambda wd, cid, v: panel.privacy_card(wd),
    "page_status": lambda wd, cid, v: panel.status_card(wd, cid),
    # 兼容历史卡片；返回格式页已移除。
    "page_format": lambda wd, cid, v: panel.settings_menu_card(wd),
    "page_skill": lambda wd, cid, v: panel.skill_card(wd, _sel(cid, "skill")),
    "page_mcp": lambda wd, cid, v: panel.mcp_card(wd, _sel(cid, "mcp")),
    "page_schedule": lambda wd, cid, v: panel.schedule_card(wd, _sel(cid, "schedule").get("task")),
}


def _usage_card(chat_id: str, wd: Path) -> dict:
    """Codex 用量查询：发 /status 并解析最小展示字段。"""
    return _codex_usage_card(chat_id, wd)


def _codex_usage_card(chat_id: str, wd: Path) -> dict:
    try:
        session.ensure(chat_id, wd, cold_wait=CONFIG.get("cold_start_wait_sec", 6))
        session.send_command(chat_id, "/status")
        time.sleep(1.2)
        text = session.capture(chat_id, lines=160)
        parsed = usage.parse_codex_status(text)
        if parsed.get("model"):
            model = parsed["model"].split("(", 1)[0].strip()
            if model:
                chatconfig.set_value(wd, "codex_effective_model", model)
        lines = parsed["lines"]
        if not lines:
            return panel.usage_card(wd, error="没有解析到 /status 用量信息。")
        return panel.usage_card(wd, lines=lines)
    except Exception as e:
        print(f"[usage] 获取失败 chat={chat_id}: {config.redact_text(e)}")
        return panel.usage_card(wd, error="无法读取 Codex 用量，请检查运行状态或服务器日志。")


def _continue_after_question(chat_id: str, wd: Path, state: dict) -> None:
    _dispatch_to_agent(
        chat_id,
        wd,
        questions.answers_prompt(state),
        f"question_{state.get('id', '')}",
        "question-card",
        [],
        apply_helper_hint=False,
        clear_fresh=False,
    )


def _handle_question_action(
    chat_id: str,
    wd: Path,
    action: str,
    value: dict,
    form_value: dict | None = None,
    input_value: str | None = None,
) -> P2CardActionTriggerResponse | None:
    if not action.startswith("agent_question_"):
        return None
    qid = str(value.get("question_id") or "")
    state = questions.load(qid) if qid else None
    if not state or state.get("chat_id") != chat_id:
        return _card_resp("问题已失效", cards.answer_card("这组问题已失效或不属于当前会话。"))
    if state.get("status") != "open":
        return _card_resp("", questions.question_card(state))

    try:
        index = int(value.get("index", state.get("current", 0)))
    except Exception:
        index = int(state.get("current", 0))
    index = max(0, min(index, len(state.get("questions", [])) - 1))

    if action == "agent_question_select":
        try:
            option_index = int(value.get("option", -1))
        except Exception:
            option_index = -1
        questions.answer_option(state, index, option_index)
        questions.save(state)
        return _card_resp("", questions.question_card(state))

    if action == "agent_question_input":
        text_answer = questions.input_value_from_form(form_value, input_value, index)
        if text_answer:
            questions.answer_text(state, index, text_answer)
            questions.save(state)
        return _card_resp("", questions.question_card(state))

    if action == "agent_question_custom":
        questions.ask_custom(state, index)
        questions.save(state)
        return _card_resp("请发送自定义答案", questions.question_card(state))

    if action == "agent_question_nav":
        text_answer = questions.input_value_from_form(form_value, input_value, index)
        if text_answer:
            questions.answer_text(state, index, text_answer)
        try:
            to = int(value.get("to", index))
        except Exception:
            to = index
        state["current"] = max(0, min(to, len(state.get("questions", [])) - 1))
        state["awaiting_text"] = None
        questions.save(state)
        return _card_resp("", questions.question_card(state))

    if action == "agent_question_submit":
        text_answer = questions.input_value_from_form(form_value, input_value, index)
        if text_answer:
            questions.answer_text(state, index, text_answer)
        if not questions.submit(state):
            questions.save(state)
            return _card_resp("还有问题未回答", questions.question_card(state))
        questions.save(state)
        threading.Thread(target=_continue_after_question, args=(chat_id, wd, copy.deepcopy(state)), daemon=True).start()
        return _card_resp("已提交", questions.question_card(state))

    if action == "agent_question_cancel":
        questions.cancel(state)
        questions.save(state)
        return _card_resp("已取消", questions.question_card(state))

    return _card_resp("", questions.question_card(state))


def on_card_action(data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    ev = data.event
    ctx = ev.context
    chat_id = ctx.open_chat_id if ctx else ""
    message_id = ctx.open_message_id if ctx else ""
    value = ev.action.value if ev.action else {}
    option = ev.action.option if ev.action else None  # 下拉框选中的值
    form_value = ev.action.form_value if ev.action else None
    input_value = ev.action.input_value if ev.action else None
    action = (value or {}).get("action")
    wd = workdir_for(chat_id) if chat_id else None
    if wd is None:
        return P2CardActionTriggerResponse({})

    # ⏹ 停止：打断当前轮（发 ESC），并把进度卡替换成明确的中断提示
    if action == "stop":
        session.interrupt(chat_id)
        workers.request_stop(chat_id)
        activity = workers.current_activity(wd, include_tools=False)
        return _card_resp("已中断", cards.stopped_card(activity))
    if action == "codex_usage":
        # 抓用量要 ensure 会话 + 发命令 + 抓屏，可能远超飞书回调超时；
        # 先秒回一张 loading 卡，后台抓完再 update_card 覆盖。
        if message_id:
            def _refresh_usage(mid: str) -> None:
                try:
                    feishu.update_card(mid, _usage_card(chat_id, wd))
                except Exception as e:
                    print(f"[card] 用量刷新失败: {e}")
            threading.Thread(target=_refresh_usage, args=(message_id,), daemon=True).start()
            return _card_resp("正在获取用量…", panel.usage_card(wd, lines=["⏳ 正在获取用量，请稍候…"]))
        return _card_resp("已刷新用量", _usage_card(chat_id, wd))
    if action == "close":
        return _card_resp("", panel.closed_card(wd, _consume_changes(chat_id)))
    if action and action.startswith("agent_question_"):
        resp = _handle_question_action(chat_id, wd, action, value, form_value, input_value)
        if resp is not None:
            return resp

    # 纯导航：原地刷新
    if action in _NAV:
        return _card_resp("", _NAV[action](wd, chat_id, value))
    # 配置切换：改完原地刷新对应卡 + toast
    if action == "set_memory":
        msg = chatconfig.switch_memory(wd, value.get("mode", "resume"))
        return _changed_for(chat_id, msg, panel.memory_card(wd))
    if action == "set_retention":
        try:
            days = int(option)
        except (TypeError, ValueError):
            days = 30
        if days not in privacy.RETENTION_OPTIONS:
            days = 30
        key = "attachment_retention_days" if value.get("kind") == "attachments" else "task_log_retention_days"
        chatconfig.set_value(wd, key, days)
        privacy.cleanup_expired(wd)
        label = "不自动清理" if days == 0 else f"保留 {days} 天"
        return _changed_for(chat_id, f"已设置：{label}", panel.privacy_card(wd))
    if action == "privacy_clear_ask":
        return _card_resp("", panel.confirm_privacy_clear_card(str(value.get("category") or "")))
    if action == "privacy_clear":
        category = str(value.get("category") or "")
        if workers.active_turn(chat_id):
            return _card_resp("当前任务执行中，请停止或等待完成后再清理", panel.privacy_card(wd))
        if category == "context":
            if session.exists(session.session_name(chat_id)):
                session.kill(chat_id)
            return _changed_for(chat_id, "已重置临时上下文；下一条消息会启动新会话", panel.privacy_card(wd))
        try:
            removed = privacy.clear_category(wd, category)
        except ValueError:
            return _card_resp("未知清理项目", panel.privacy_card(wd))
        return _changed_for(chat_id, f"清理完成：移除 {removed} 项", panel.privacy_card(wd))
    if action == "set_reply":
        return _card_resp("群聊回复现已自动判断", panel.reply_card(wd))
    if action == "set_format":
        return _card_resp("返回格式已固定为 Markdown", panel.settings_menu_card(wd))
    if action == "help_doc":
        return _card_resp("帮助文档入口已移除", panel.main_menu_card(wd, chat_id))
    if action == "dismiss_skill_updates":
        hub.dismiss_skill_updates(wd)
        return _card_resp("Skill 已动态更新", panel.main_menu_card(wd, chat_id))
    if action in {"stage_codex_model", "set_codex_model"}:
        draft = _model_draft(chat_id, wd)
        draft["model"] = value.get("model", chatconfig.DEFAULTS["codex_model"])
        draft["effort"] = chatconfig.normalize_codex_effort(draft["model"], draft.get("effort", ""))
        return _card_resp("已暂存模型，请继续选择 Effort", panel.model_card(wd, draft))
    if action == "stage_codex_effort":
        draft = _model_draft(chat_id, wd)
        draft["effort"] = chatconfig.normalize_codex_effort(draft["model"], value.get("effort", ""))
        return _card_resp("已暂存 Effort，确认后点击应用并重启", panel.model_card(wd, draft))
    if action == "reset_codex_draft":
        draft = _model_draft(chat_id, wd, reset=True)
        return _card_resp("已取消未应用的更改", panel.model_card(wd, draft))
    if action == "apply_codex_settings":
        draft = dict(_model_draft(chat_id, wd))
        msg = _apply_codex_settings(chat_id, wd, draft["model"], draft["effort"])
        _model_draft(chat_id, wd, reset=True)
        return _changed_for(chat_id, msg, panel.model_card(wd, _model_draft(chat_id, wd)))
    if action in {"set_engine_codex", "set_model", "set_effort"}:
        # 兼容历史消息卡片，不再暴露旧的引擎/模型选项。
        chatconfig.set_value(wd, "engine", "codex")
        return _card_resp("", panel.model_card(wd, _model_draft(chat_id, wd)))

    # —— 定时任务卡 ——
    if action == "sel_schedule":
        _sel(chat_id, "schedule")["task"] = option
        return _card_resp("", panel.schedule_card(wd, option))
    if action and action.startswith("schedule_"):
        task_id = str(value.get("task_id") or "")
        try:
            task = scheduled.get_task(wd, task_id)
            if task is None:
                return _card_resp("任务已不存在", panel.schedule_card(wd))
            if action == "schedule_run":
                scheduled.request_run(wd, task_id)
                return _changed_for(chat_id, "已提交立即运行请求", panel.schedule_card(wd, task_id))
            if action == "schedule_pause":
                scheduled.pause_task(wd, task_id)
                return _changed_for(chat_id, "已暂停后续调度", panel.schedule_card(wd, task_id))
            if action == "schedule_resume":
                scheduled.resume_task(wd, task_id)
                return _changed_for(chat_id, "已恢复调度", panel.schedule_card(wd, task_id))
            if action == "schedule_delete_ask":
                return _card_resp("", panel.confirm_schedule_delete_card(task))
            if action == "schedule_delete":
                scheduled.delete_task(wd, task_id)
                _sel(chat_id, "schedule").pop("task", None)
                return _changed_for(chat_id, f"已删除定时任务：{task['name']}", panel.schedule_card(wd))
        except Exception as exc:
            return _card_resp(f"操作失败：{exc}", panel.schedule_card(wd, task_id))

    # —— hub 卡：下拉选择 ——
    if action and action.startswith("sel_skill_"):
        _sel(chat_id, "skill")[action[len("sel_skill_"):]] = option
        return _card_resp("", panel.skill_card(wd, _sel(chat_id, "skill")))
    if action and action.startswith("sel_mcp_"):
        _sel(chat_id, "mcp")[action[len("sel_mcp_"):]] = option
        return _card_resp("", panel.mcp_card(wd, _sel(chat_id, "mcp")))

    # —— hub 卡：Skill 使用/禁用/发布（Codex 自动检测变更）——
    name = value.get("name", "")
    if action == "skill_gen_use":
        hub.load_skill(wd, name)
        return _changed_for(chat_id, f"✅ 已使用 {name}（动态生效）", panel.skill_card(wd, _sel(chat_id, "skill")))
    if action == "skill_gen_disable":
        hub.unload_skill(wd, name)
        return _changed_for(chat_id, f"🚫 已禁用 {name}（动态生效）", panel.skill_card(wd, _sel(chat_id, "skill")))
    if action == "skill_spec_use":
        hub.enable_special_skill(wd, name)
        return _changed_for(chat_id, f"✅ 已启用 {name}（动态生效）", panel.skill_card(wd, _sel(chat_id, "skill")))
    if action == "skill_spec_disable":
        hub.disable_special_skill(wd, name)
        return _changed_for(chat_id, f"🚫 已禁用 {name}（动态生效）", panel.skill_card(wd, _sel(chat_id, "skill")))
    if action == "skill_spec_delete_ask":
        return _card_resp("", panel.confirm_delete_card("skill", name))
    if action == "skill_spec_delete":
        hub.delete_special_skill(wd, name)
        return _changed_for(chat_id, f"🗑 已删除 {name}（动态生效）", panel.skill_card(wd, _sel(chat_id, "skill")))
    if action == "skill_publish":
        report = hub.publish_skill(wd, name)
        if report.ok:
            try:
                bitable.ensure_catalog()
            except Exception:
                pass
        detail = "；".join(report.errors[:2])
        message = "已验证并升级为通用 Skill" if report.ok else f"发布失败：{detail}"
        return _changed_for(chat_id, message,
                            panel.skill_card(wd, _sel(chat_id, "skill")))

    # —— hub 卡：mcp 使用/禁用/发布 ——
    if action == "mcp_gen_use":
        hub.load_mcp(wd, name)
        hub.mark_resource_updates(wd, [f"mcp:{name}"])
        return _changed_for(chat_id, f"已使用 {name}（待重启当前会话载入）", panel.mcp_card(wd, _sel(chat_id, "mcp")))
    if action == "mcp_gen_disable":
        hub.unload_mcp(wd, name)
        hub.mark_resource_updates(wd, [f"mcp:{name}:removed"])
        return _changed_for(chat_id, f"已禁用 {name}（待重启当前会话载入）", panel.mcp_card(wd, _sel(chat_id, "mcp")))
    if action == "mcp_spec_use":
        hub.enable_special_mcp(wd, name)
        hub.mark_resource_updates(wd, [f"mcp:{name}"])
        return _changed_for(chat_id, f"已启用 {name}（待重启当前会话载入）", panel.mcp_card(wd, _sel(chat_id, "mcp")))
    if action == "mcp_spec_disable":
        hub.disable_special_mcp(wd, name)
        hub.mark_resource_updates(wd, [f"mcp:{name}:removed"])
        return _changed_for(chat_id, f"已禁用 {name}（待重启当前会话载入）", panel.mcp_card(wd, _sel(chat_id, "mcp")))
    if action == "mcp_spec_delete_ask":
        return _card_resp("", panel.confirm_delete_card("mcp", name))
    if action == "mcp_spec_delete":
        hub.delete_special_mcp(wd, name)
        hub.mark_resource_updates(wd, [f"mcp:{name}:removed"])
        return _changed_for(chat_id, f"🗑 已删除 {name}（待重启当前会话载入）", panel.mcp_card(wd, _sel(chat_id, "mcp")))
    if action == "mcp_publish":
        report = hub.publish_mcp(wd, name)
        if report.ok:
            try:
                bitable.ensure_catalog()
            except Exception:
                pass
        detail = "；".join(report.errors[:2])
        message = "已验证并升级为通用 MCP" if report.ok else f"发布失败：{detail}"
        return _changed_for(chat_id, message,
                            panel.mcp_card(wd, _sel(chat_id, "mcp")))

    if action == "apply_restart":
        threading.Thread(target=_restart_and_notify, args=(chat_id, wd, "用户确认载入配置"), daemon=True).start()
        if value.get("page") == "mcp":
            return _changed_for(chat_id, "🔄 正在重启当前会话并载入 MCP", panel.mcp_card(wd, _sel(chat_id, "mcp")))
        return _changed_for(chat_id, "🔄 正在重启当前会话并载入配置", panel.main_menu_card(wd, chat_id))

    # 终结类：撤回卡 + 另发结论（耗时活儿丢后台线程，避免回调超时）
    print(f"[card] chat={chat_id} action={action} (撤回+结论)")
    try:
        if message_id:
            feishu.recall(message_id)
    except Exception as e:
        print(f"[card] 撤回失败: {e}")

    if action == "restart":
        threading.Thread(target=_restart_and_notify, args=(chat_id, wd, "用户手动重启当前会话"), daemon=True).start()
    else:
        feishu.send_text(chat_id, "未知操作")

    return P2CardActionTriggerResponse({})


_WELCOME_TEXT = (
    "👋 我已加入。只有一位真人时可直接对话；多人群请 @我。"
    "发送 /help 打开控制面板，/status 查看状态，/privacy 管理本机数据。"
)


def _send_welcome(chat_id: str, workdir: Path) -> None:
    try:
        feishu.send_card(chat_id, panel.welcome_card(workdir, chat_id))
    except Exception as exc:
        print(f"[welcome] 引导卡发送失败，降级为文本: {config.redact_text(exc)}")
        feishu.send_text(chat_id, _WELCOME_TEXT)


def on_bot_added(data) -> None:
    """机器人被邀请进群：仅首次登记时欢迎（避免改名/重投等再次触发重复欢迎）。"""
    chat_id = data.event.chat_id
    is_new = chat_id not in load_registry().get("chats", {})
    wd = workdir_for(chat_id)
    if wd is None:
        return
    threading.Thread(
        target=feishu.chat_human_member_count,
        args=(chat_id,),
        daemon=True,
    ).start()
    if not is_new:
        print(f"[join] 群 {chat_id} 已登记，跳过欢迎")
        return
    print(f"[join] 新群 {chat_id} 欢迎+登记")
    try:
        _send_welcome(chat_id, wd)
    except Exception as e:
        print(f"[join] 欢迎失败: {e}")
    if bitable.enabled():
        threading.Thread(target=lambda: bitable.sync_group(chat_id, wd), daemon=True).start()


def on_bitable_change(data) -> None:
    """多维表格记录变更 → 重新对齐各群 skill/mcp 并按需重启。"""
    print("[bitable] 记录变更，应用…")
    try:
        print("[bitable]", apply_from_bitable())
    except Exception as e:
        print(f"[bitable] 应用失败: {e}")


def build_client() -> lark.ws.Client:
    app_id = config.require(CONFIG, "feishu", "app_id")
    app_secret = config.require(CONFIG, "feishu", "app_secret")
    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_card_action_trigger(on_card_action)
        .register_p2_im_message_recalled_v1(lambda data: None)  # 静音撤回回推噪音
        .register_p2_drive_file_bitable_record_changed_v1(on_bitable_change)
        .register_p2_im_chat_member_bot_added_v1(on_bot_added)  # 被邀请进群 → 欢迎+登记
        .build()
    )
    client = lark.ws.Client(app_id, app_secret, event_handler=handler, log_level=lark.LogLevel.INFO)
    # Lark SDK 默认失败后等待 120s；短暂的 SSL/WS 抖动不应让机器人长时间离线。
    client._reconnect_interval = max(1, int(CONFIG.get("feishu_ws_reconnect_interval_sec", 10)))
    client._reconnect_nonce = max(0, int(CONFIG.get("feishu_ws_reconnect_nonce_sec", 3)))
    return client


if __name__ == "__main__":
    _acquire_single_instance()
    print("chat-agent 启动：飞书长连接 + tmux + codex")
    threading.Thread(target=_hub_reconcile_loop, name="hub-reconcile", daemon=True).start()
    build_client().start()
