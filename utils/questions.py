"""Non-blocking Feishu question cards for agent clarification flows."""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from . import cards

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "state" / "questions"


def _safe(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", value)


def question_path(question_id: str) -> Path:
    return STATE_DIR / f"{_safe(question_id)}.json"


def _now() -> float:
    return time.time()


def normalize_questions(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    questions = []
    for idx, item in enumerate(raw, 1):
        text = str(item.get("text") or item.get("question") or "").strip()
        if not text:
            continue
        options = [str(x).strip() for x in item.get("options", []) if str(x).strip()]
        questions.append({
            "id": str(item.get("id") or f"q{idx}"),
            "text": text,
            "options": options,
            "required": bool(item.get("required", True)),
            "answer": str(item.get("answer") or ""),
            "answer_type": str(item.get("answer_type") or ""),
            "answered_at": item.get("answered_at"),
        })
    return questions


def create(chat_id: str, title: str, questions: list[dict[str, Any]], *, prompt: str = "") -> dict:
    items = normalize_questions(questions)
    if not items:
        raise ValueError("at least one question is required")
    state = {
        "id": uuid.uuid4().hex,
        "chat_id": chat_id,
        "title": title.strip() or "需要确认",
        "prompt": prompt.strip(),
        "status": "open",
        "current": 0,
        "questions": items,
        "awaiting_text": None,
        "message_id": "",
        "created_at": _now(),
        "updated_at": _now(),
        "submitted_at": None,
    }
    save(state)
    return state


def load(question_id: str) -> dict | None:
    p = question_path(question_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state["updated_at"] = _now()
    question_path(str(state["id"])).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def set_message_id(state: dict, message_id: str) -> dict:
    state["message_id"] = message_id
    save(state)
    return state


def find_awaiting(chat_id: str) -> dict | None:
    if not STATE_DIR.is_dir():
        return None
    best = None
    best_time = -1.0
    for path in STATE_DIR.glob("*.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if state.get("chat_id") != chat_id or state.get("status") != "open":
            continue
        if state.get("awaiting_text") is None:
            continue
        updated = float(state.get("updated_at") or 0)
        if updated > best_time:
            best, best_time = state, updated
    return best


def answer_option(state: dict, index: int, option_index: int) -> None:
    q = state["questions"][index]
    options = q.get("options") or []
    if option_index < 0 or option_index >= len(options):
        return
    q["answer"] = str(options[option_index])
    q["answer_type"] = "option"
    q["answered_at"] = _now()
    state["awaiting_text"] = None
    state["current"] = index + 1 if index < len(state["questions"]) - 1 else index


def ask_custom(state: dict, index: int) -> None:
    state["current"] = index
    state["awaiting_text"] = {"index": index}


def answer_custom(state: dict, text: str) -> None:
    awaiting = state.get("awaiting_text") or {}
    index = int(awaiting.get("index", state.get("current", 0)))
    index = max(0, min(index, len(state["questions"]) - 1))
    q = state["questions"][index]
    q["answer"] = text.strip()
    q["answer_type"] = "text"
    q["answered_at"] = _now()
    state["awaiting_text"] = None
    if index < len(state["questions"]) - 1:
        state["current"] = index + 1
    else:
        state["current"] = index


def answer_text(state: dict, index: int, text: str) -> bool:
    text = text.strip()
    if not text:
        return False
    index = max(0, min(index, len(state["questions"]) - 1))
    q = state["questions"][index]
    q["answer"] = text
    q["answer_type"] = "text"
    q["answered_at"] = _now()
    state["awaiting_text"] = None
    return True


def unanswered_index(state: dict) -> int | None:
    for idx, q in enumerate(state.get("questions", [])):
        if q.get("required", True) and not str(q.get("answer") or "").strip():
            return idx
    return None


def submit(state: dict) -> bool:
    missing = unanswered_index(state)
    if missing is not None:
        state["current"] = missing
        return False
    state["status"] = "submitted"
    state["submitted_at"] = _now()
    state["awaiting_text"] = None
    return True


def cancel(state: dict) -> None:
    state["status"] = "cancelled"
    state["awaiting_text"] = None


def answers_prompt(state: dict) -> str:
    lines = [
        f"用户已通过飞书问题卡片回答了你的澄清问题（question_id={state.get('id', '')}）。",
        "请基于这些答案继续处理上一轮任务。",
        "",
    ]
    if state.get("prompt"):
        lines.extend(["问题说明：", str(state["prompt"]).strip(), ""])
    for idx, q in enumerate(state.get("questions", []), 1):
        answer = str(q.get("answer") or "").strip() or "（未填写）"
        lines.append(f"{idx}. {q.get('text', '')}")
        lines.append(f"回答：{answer}")
    return "\n".join(lines).strip()


def _clip(text: str, limit: int = 32) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _answer_count(state: dict) -> tuple[int, int]:
    qs = state.get("questions", [])
    answered = sum(1 for q in qs if str(q.get("answer") or "").strip())
    return answered, len(qs)


def _input_name(index: int) -> str:
    return f"question_input_{index}"


def input_value_from_form(form_value: dict | None, input_value: str | None, index: int) -> str:
    if isinstance(form_value, dict):
        value = form_value.get(_input_name(index))
        if value is None and len(form_value) == 1:
            value = next(iter(form_value.values()))
        if value is not None:
            return str(value).strip()
    return str(input_value or "").strip()


def _input(question_id: str, index: int, default_value: str = "") -> dict:
    el = {
        "tag": "input",
        "name": _input_name(index),
        "placeholder": {"tag": "plain_text", "content": "也可以直接输入自己的意见"},
        "value": {
            "action": "agent_question_input",
            "question_id": question_id,
            "index": index,
        },
    }
    if default_value:
        el["default_value"] = default_value
    return el


def question_card(state: dict) -> dict:
    qs = state.get("questions", [])
    total = len(qs)
    idx = max(0, min(int(state.get("current", 0)), max(total - 1, 0)))
    answered, _ = _answer_count(state)
    if state.get("status") == "submitted":
        return submitted_card(state)
    if state.get("status") == "cancelled":
        return cards.card("提问已取消", [cards.div("这组问题已取消，不会继续发送给 agent。")], "grey")

    q = qs[idx]
    title = state.get("title") or "需要确认"
    body = [cards.div(f"**问题 {idx + 1} / {total}**　已回答 {answered} 个")]
    if state.get("prompt"):
        body.append(cards.div(str(state["prompt"])[:900]))
    body.append(cards.hr())
    body.append(cards.div(f"**{idx + 1}. {q.get('text', '')}**"))
    if q.get("answer"):
        body.append(cards.div(f"当前答案：**{q.get('answer')}**"))
    if state.get("awaiting_text"):
        body.append(cards.div("请直接在当前飞书对话里发送这一题的补充答案。"))

    opts = q.get("options") or []
    for opt_idx, opt in enumerate(opts):
        body.append(cards.act(cards.btn(f"{opt_idx + 1}. {_clip(opt, 48)}", {
            "action": "agent_question_select",
            "question_id": state["id"],
            "index": idx,
            "option": opt_idx,
        }, "primary" if opt == q.get("answer") else "default")))
    body.append(_input(state["id"], idx, q.get("answer", "") if q.get("answer_type") == "text" else ""))
    body.append(cards.act(cards.btn("输入自己的意见", {
        "action": "agent_question_custom",
        "question_id": state["id"],
        "index": idx,
    })))

    nav = []
    if idx > 0:
        nav.append(cards.btn("上一题", {"action": "agent_question_nav", "question_id": state["id"], "to": idx - 1}))
    if idx < total - 1:
        nav.append(cards.btn("下一题", {"action": "agent_question_nav", "question_id": state["id"], "to": idx + 1}))
    else:
        nav.append(cards.btn("提交", {"action": "agent_question_submit", "question_id": state["id"], "index": idx}, "primary"))
    if nav:
        body.append(cards.act(*nav))
    return cards.card(f"{title} · {idx + 1}/{total}", body, "blue")


def submitted_card(state: dict) -> dict:
    body = [cards.div("答案已提交，agent 会基于这些选择继续处理。"), cards.hr()]
    for idx, q in enumerate(state.get("questions", []), 1):
        answer = str(q.get("answer") or "").strip() or "（未填写）"
        body.append(cards.div(f"**{idx}. {q.get('text', '')}**\n{answer}"))
    return cards.card(state.get("title") or "已提交", body, "green")
