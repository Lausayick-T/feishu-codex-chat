"""
workers.py —— 后台线程：进度卡更新 + codex 回复捕获。

回复统一走 state/<chat>.final：
  - codex_reply_worker tail session 文件、抓 task_complete 写 .final
progress_worker 消费 .final，把进度卡原地换成回复。
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from . import cards, config, feishu, session

_SEG_LIMIT = 3500
def _strip_md(t: str) -> str:
    t = re.sub(r"```[^\n]*\n?", "", t)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"\*\*([^*]+)\*\*", r"\1", t)
    t = re.sub(r"\*([^*]+)\*", r"\1", t)
    t = re.sub(r"__([^_]+)__", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"^\s{0,3}[-*+]\s+", "• ", t, flags=re.M)
    return t


def _split(text: str, limit: int = _SEG_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(line) > limit:  # 单行超长，硬切
            if cur:
                parts.append(cur); cur = ""
            for i in range(0, len(line), limit):
                parts.append(line[i:i + limit])
            continue
        if len(cur) + len(line) > limit and cur:
            parts.append(cur); cur = ""
        cur += line
    if cur:
        parts.append(cur)
    return parts


def deliver(chat_id: str, message_id: str, text: str, fmt: str) -> None:
    """按返回格式把回复发出去。md→卡片渲染；raw/plain→纯文本（plain 去 Markdown）。长则分段。"""
    if fmt == "plain":
        text = _strip_md(text)
    parts = _split(text)
    if fmt == "md":
        feishu.update_card(message_id, cards.markdown_card(parts[0]))
        for p in parts[1:]:
            feishu.send_card(chat_id, cards.markdown_card(p))
    else:  # raw / plain → 纯文本，首段更新到思考卡，其余分段发文本
        feishu.update_card(message_id, cards.text_card(parts[0]))
        for p in parts[1:]:
            feishu.send_text(chat_id, p)


def deliver_new(chat_id: str, text: str, fmt: str) -> None:
    """发送一条新的最终回复。"""
    if fmt == "plain":
        text = _strip_md(text)
    parts = _split(text)
    if fmt == "md":
        for p in parts:
            feishu.send_card(chat_id, cards.markdown_card(p))
    else:
        for p in parts:
            feishu.send_text(chat_id, p)


def deliver_with_fallback(chat_id: str, message_id: str, text: str, fmt: str) -> None:
    """优先原地更新进度卡；失败时降级发送新消息，避免终稿丢失。"""
    try:
        deliver(chat_id, message_id, text, fmt)
    except Exception as e:
        print(f"[progress] 原地更新终稿失败，改发新消息: {e}")
        deliver_new(chat_id, text, fmt)


ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "state"
_CONFIG = config.load()
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_TURNS: dict[str, dict] = {}


def _safe(chat_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", chat_id)


def final_path(chat_id: str) -> Path:
    return STATE / f"{_safe(chat_id)}.final"


def stop_path(chat_id: str) -> Path:
    return STATE / f"{_safe(chat_id)}.stop"


def sent_path(chat_id: str) -> Path:
    return STATE / f"{_safe(chat_id)}.sent"


def begin_active_turn(chat_id: str) -> bool:
    """原子地占用一个对话轮次；False 表示已有任务在执行。"""
    with _ACTIVE_LOCK:
        if chat_id in _ACTIVE_TURNS:
            return False
        started_at = time.time()
        _ACTIVE_TURNS[chat_id] = {
            "message_id": "",
            "started_at": started_at,
            "task_started_at": started_at,
            "starting": False,
            "accepting": False,
            "handoff": False,
            "activity": "收到，准备中…",
            "supplement_count": 0,
            "supplement_note": "",
            "pending_prompts": [],
        }
        return True


def set_active_card(chat_id: str, message_id: str) -> None:
    with _ACTIVE_LOCK:
        if state := _ACTIVE_TURNS.get(chat_id):
            state["message_id"] = message_id


def rotate_active_card(chat_id: str, message_id: str) -> dict | None:
    """把后续进度/终稿切到追问后的新卡片，并为新卡片重新计时。"""
    with _ACTIVE_LOCK:
        state = _ACTIVE_TURNS.get(chat_id)
        if state is None:
            return None
        state["message_id"] = message_id
        state["started_at"] = time.time()
        state["supplement_note"] = ""
        state["handoff"] = False
        return dict(state)


def cancel_active_handoff(chat_id: str) -> None:
    """新卡创建失败时恢复旧卡作为回复目标。"""
    with _ACTIVE_LOCK:
        if state := _ACTIVE_TURNS.get(chat_id):
            state["handoff"] = False


def set_active_starting(chat_id: str, starting: bool) -> None:
    with _ACTIVE_LOCK:
        if state := _ACTIVE_TURNS.get(chat_id):
            state["starting"] = starting


def set_active_activity(chat_id: str, activity: str) -> None:
    with _ACTIVE_LOCK:
        if state := _ACTIVE_TURNS.get(chat_id):
            state["activity"] = activity


def append_active_supplement(chat_id: str, prompt: str) -> dict | None:
    """登记当前轮追问并冻结旧卡；冷启动期间同时把 prompt 排队。"""
    with _ACTIVE_LOCK:
        state = _ACTIVE_TURNS.get(chat_id)
        if state is None:
            return None
        state["supplement_count"] += 1
        state["supplement_note"] = "用户进行了额外输入"
        state["handoff"] = True
        accepting = bool(state["accepting"])
        if not accepting:
            state["pending_prompts"].append(prompt)
        return dict(state)


def activate_turn(chat_id: str) -> list[str]:
    """Codex 首条 prompt 已发送；返回冷启动期间排队的补充。"""
    with _ACTIVE_LOCK:
        state = _ACTIVE_TURNS.get(chat_id)
        if state is None:
            return []
        state["starting"] = False
        state["accepting"] = True
        pending = list(state["pending_prompts"])
        state["pending_prompts"].clear()
        return pending


def active_turn(chat_id: str) -> dict | None:
    with _ACTIVE_LOCK:
        state = _ACTIVE_TURNS.get(chat_id)
        return dict(state) if state is not None else None


def end_active_turn(chat_id: str) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE_TURNS.pop(chat_id, None)


def request_stop(chat_id: str) -> None:
    """请求停止：进度线程看到后把当前进度卡替换为已中断提示。"""
    STATE.mkdir(exist_ok=True)
    stop_path(chat_id).write_text("1", encoding="utf-8")


def clear_final(chat_id: str) -> None:
    STATE.mkdir(exist_ok=True)
    final_path(chat_id).unlink(missing_ok=True)
    stop_path(chat_id).unlink(missing_ok=True)
    sent_path(chat_id).unlink(missing_ok=True)


def _shorten(text: str, limit: int = 420) -> str:
    text = re.sub(r"\s+", " ", str(text)).strip()
    if len(text) > limit:
        return text[:limit].rstrip() + "..."
    return text


def _event_ts(o: dict) -> float | None:
    ts = o.get("timestamp")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _latest_activity(workdir: Path, *, include_tools: bool = True, after: float | None = None) -> str:
    """读取 Codex session，推断当前步骤级进度。"""
    return _latest_codex_activity(workdir, include_tools=include_tools, after=after)


def _latest_codex_activity(workdir: Path, *, include_tools: bool = True, after: float | None = None) -> str:
    sess = session.latest_codex_session(workdir)
    if not sess:
        return "准备执行..."
    try:
        recent = deque(sess.open(encoding="utf-8"), maxlen=80)
    except Exception:
        return "准备执行..."
    fallback = ""
    for line in reversed(recent):
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if after is not None:
            ts = _event_ts(o)
            if ts is not None and ts < after:
                continue
        typ = o.get("type")
        payload = o.get("payload") or {}
        ptyp = payload.get("type")
        text = _codex_visible_progress_text(typ, payload, ptyp)
        if text:
            return text
        if typ == "event_msg" and ptyp == "task_complete":
            fallback = fallback or "正在整理结果..."
        if typ == "event_msg" and ptyp in {"mcp_tool_call_end", "patch_apply_end"}:
            fallback = fallback or "处理工具结果中..."
        if typ == "response_item":
            if ptyp in {"function_call", "custom_tool_call"}:
                name = payload.get("name") or ("apply_patch" if ptyp == "custom_tool_call" else "工具")
                fallback = fallback or (f"正在使用 {name}..." if include_tools else "正在执行操作...")
            if ptyp in {"function_call_output", "custom_tool_call_output"}:
                fallback = fallback or "处理工具结果中..."
            if ptyp == "reasoning":
                fallback = fallback or "分析任务中..."
    return fallback or "准备执行..."


def _codex_visible_progress_text(typ: str, payload: dict, ptyp: str | None) -> str:
    """取 Codex 自己输出的可见阶段文本；final_answer 交给终稿路径处理。"""
    if payload.get("phase") == "final_answer":
        return ""
    if typ == "event_msg" and ptyp == "agent_message":
        msg = str(payload.get("message") or "").strip()
        return _shorten(msg, 900) if msg else ""
    if typ == "response_item" and ptyp == "message":
        parts = payload.get("content") or []
        texts = [
            str(c.get("text", "")).strip()
            for c in parts
            if isinstance(c, dict) and c.get("type") == "output_text" and str(c.get("text", "")).strip()
        ]
        return _shorten("\n".join(texts), 900) if texts else ""
    return ""


def current_activity(workdir: Path, *, include_tools: bool = True) -> str:
    return _latest_activity(workdir, include_tools=include_tools)


CODEX_REPLY_TIMEOUT = max(1800, int(_CONFIG.get("codex_reply_timeout_sec", 7200)))


def _active_size(workdir: Path) -> int:
    """当前活跃 Codex session 的字节数，用于判断 agent 是否还在干活。"""
    current = session.latest_codex_session(workdir)
    if not current:
        return -1
    try:
        return current.stat().st_size
    except OSError:
        return -1


def _latest_completed_message(workdir: Path, *, after: float) -> str:
    """从本轮最新 rollout 补偿提取终稿，用于 tail worker 切换文件失败时自愈。"""
    sess = session.latest_codex_session(workdir, after=after - 10)
    if not sess:
        return ""
    try:
        recent = deque(sess.open(encoding="utf-8"), maxlen=240)
    except OSError:
        return ""
    for line in reversed(recent):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = _event_ts(event)
        if ts is not None and ts < after - 2:
            continue
        answer = _final_event_message(event)
        if answer:
            return answer
    return ""


def progress_worker(chat_id: str, message_id: str, workdir: Path) -> None:
    """边干边把卡片原地更新成进度；终稿(.final)出现后换成回复。
    长推理可能数分钟不写 transcript，不再把静默时间当成失败。"""
    fp = final_path(chat_id)
    active = active_turn(chat_id) or {}
    fallback_start = float(active.get("started_at") or time.time())
    task_start = float(active.get("task_started_at") or fallback_start)
    last_change = time.time()
    last_size = -2
    last_card = None
    last_message_id = ""
    last_recovery_check = 0.0
    sp = stop_path(chat_id)
    while True:
        active = active_turn(chat_id)
        if active is not None and active.get("handoff"):
            time.sleep(0.1)
            continue
        current_message_id = str((active or {}).get("message_id") or message_id)
        card_start = float((active or {}).get("started_at") or fallback_start)
        if sp.exists():  # 用户点了停止：明确覆盖为已中断提示
            sp.unlink(missing_ok=True)
            if current_message_id:
                try:
                    activity = _latest_activity(workdir, include_tools=False, after=card_start)
                    feishu.update_card(
                        current_message_id,
                        cards.stopped_card(activity, int(time.time() - card_start)),
                    )
                except Exception as e:
                    print(f"[progress] 中断卡更新失败: {e}")
            end_active_turn(chat_id)
            return
        if fp.exists():
            ans = fp.read_text(encoding="utf-8")
            fp.unlink(missing_ok=True)
            fmt = "md"
            try:
                sent_summary = ""
                if sent_path(chat_id).exists():
                    sent_summary = sent_path(chat_id).read_text(encoding="utf-8").strip() or "已发送。"
                    sent_path(chat_id).unlink(missing_ok=True)
                # 主动发送文件/图片只用于避免终稿为空，不再抑制 Codex 的总结、结论和重要提醒。
                answer = ans.strip() or sent_summary or "已完成。"
                if current_message_id:
                    deliver_with_fallback(chat_id, current_message_id, answer, fmt)
                else:
                    deliver_new(chat_id, answer, fmt)
            except Exception as e:
                print(f"[progress] 终稿发送失败: {e}")
            end_active_turn(chat_id)
            return
        if active is None:
            return
        sz = _active_size(workdir)
        if sz != last_size:  # 还在产出 → 续期
            last_size = sz
            last_change = time.time()
        elapsed = time.time() - task_start
        silence = time.time() - last_change
        # Codex 已回到输入提示符却没有 .final：从 rollout 补偿恢复，
        # 避免因新会话首条消息才创建 rollout 而漏掉终稿。
        if silence >= 5 and time.time() - last_recovery_check >= 5:
            last_recovery_check = time.time()
            try:
                if session.codex_tui_ready(chat_id):
                    recovered = _latest_completed_message(workdir, after=task_start)
                    if recovered:
                        fp.write_text(recovered, encoding="utf-8")
                        print(f"[progress] 补偿恢复终稿 chat={chat_id} len={len(recovered)}")
                        continue
            except Exception as exc:
                print(f"[progress] 终稿补偿检查失败 chat={chat_id}: {exc}")
        if elapsed > CODEX_REPLY_TIMEOUT:
            if current_message_id:
                try:
                    minutes = CODEX_REPLY_TIMEOUT // 60
                    feishu.update_card(current_message_id, cards.answer_card(
                        f"⚠️ 任务执行已超过 {minutes} 分钟，已停止在飞书等待结果。Codex 可能仍在后台运行。"
                    ))
                except Exception:
                    pass
            end_active_turn(chat_id)
            return
        if not current_message_id:
            time.sleep(2)
            continue
        activity = _latest_activity(workdir, include_tools=False, after=card_start)
        if activity == "准备执行..." and active.get("activity"):
            activity = str(active["activity"])
        set_active_activity(chat_id, activity)
        active = active_turn(chat_id) or active
        elapsed_seconds = int(time.time() - card_start)
        supplement = str(active.get("supplement_note") or "")
        if active.get("starting"):
            card = cards.codex_starting_card(elapsed_seconds, supplement)
        else:
            card = cards.thinking_card(activity, elapsed_seconds, supplement)
        if current_message_id != last_message_id:
            last_message_id = current_message_id
            last_card = None
        if card != last_card:
            try:
                feishu.update_card(current_message_id, card)
                last_card = card
            except Exception as e:
                print(f"[progress] 进度更新失败: {e}")
        time.sleep(2)


def _count_task_complete(path: Path) -> int:
    try:
        return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if '"task_complete"' in line)
    except Exception:
        return 0


def _file_size(path: Path | None) -> int:
    if not path:
        return 0
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _file_mtime(path: Path | None) -> float:
    if not path:
        return 0.0
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def codex_reply_baseline(workdir: Path) -> tuple[Path | None, int, int]:
    """发送消息前采样，避免 worker 启动稍晚时漏掉很快完成的 Codex 回复。"""
    sess = session.latest_codex_session(workdir)
    return sess, (_count_task_complete(sess) if sess else 0), _file_size(sess)


def _last_codex_message(path: Path) -> str:
    msg = ""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if '"task_complete"' not in line:
                continue
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            m = (o.get("payload") or {}).get("last_agent_message")
            if m:
                msg = m
    except Exception:
        pass
    return msg


def _read_new_codex_events(path: Path, offset: int) -> tuple[list[dict], int]:
    try:
        size = path.stat().st_size
        if offset > size:
            offset = 0
        events = []
        with path.open("r", encoding="utf-8") as f:
            f.seek(offset)
            for line in f:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            return events, f.tell()
    except Exception:
        return [], offset


def _final_event_message(o: dict) -> str:
    payload = o.get("payload") or {}
    if o.get("type") == "event_msg" and payload.get("type") == "task_complete":
        return str(payload.get("last_agent_message") or "").strip()
    if (
        o.get("type") == "event_msg"
        and payload.get("type") == "agent_message"
        and payload.get("phase") == "final_answer"
    ):
        return str(payload.get("message") or "").strip()
    if (
        o.get("type") == "response_item"
        and payload.get("type") == "message"
        and payload.get("phase") == "final_answer"
    ):
        texts = [
            str(item.get("text") or "").strip()
            for item in payload.get("content") or []
            if isinstance(item, dict) and item.get("type") == "output_text"
        ]
        return "\n".join(text for text in texts if text)
    return ""


def codex_reply_worker(chat_id: str, workdir: Path, baseline: tuple[Path | None, int, int] | None = None) -> None:
    """Tail 本轮 rollout，捕获 final_answer/task_complete 并写入 .final。"""
    fp = final_path(chat_id)
    sess, _, offset = baseline or (None, 0, 0)
    discover_new_rollout = sess is not None
    rollout_after = _file_mtime(sess)
    if not sess:
        for _ in range(30):  # 等 codex 启动后创建 session 文件
            sess = session.latest_codex_session(workdir)
            if sess:
                offset = _file_size(sess)
                break
            time.sleep(1)
    if not sess:
        print(f"[codex] 找不到 session 文件 chat={chat_id}")
        return
    start = time.time()
    while time.time() - start < CODEX_REPLY_TIMEOUT:
        if stop_path(chat_id).exists():
            return
        # Codex 冷启动后可能到首条用户消息才创建 rollout。baseline 此时
        # 会指向上一个会话，必须显式排除旧文件查找新 rollout。
        if discover_new_rollout:
            replacement = session.latest_codex_session(
                workdir, after=rollout_after + 0.000001, exclude=sess
            )
            if replacement is not None:
                sess, offset = replacement, 0
                discover_new_rollout = False
                print(f"[codex] 切换到新 rollout chat={chat_id} file={sess.name}")
        cur = session.latest_codex_session(workdir) or sess
        if cur != sess and not discover_new_rollout:
            sess, offset = cur, 0
        events, offset = _read_new_codex_events(sess, offset)
        for event in events:
            ans = _final_event_message(event)
            if ans:
                fp.write_text(ans, encoding="utf-8")
                print(f"[codex] 终稿写入 chat={chat_id} len={len(ans)}")
                return
        time.sleep(2)
    print(f"[codex] 等待回复超时 chat={chat_id}")
