"""
cards.py —— 通用卡片构建原语（无业务含义，可被控制面板 / skill-mcp / 提问交互等复用）。

只放与具体功能无关的“积木”：按钮、按钮行、卡片外壳，以及对话级的思考/回复卡。
控制面板专属的那组卡片（带 返回/关闭）见 utils/panel.py。
"""

from __future__ import annotations
import re


def btn(text: str, value: dict, t: str = "default") -> dict:
    return {"tag": "button", "text": {"tag": "plain_text", "content": text}, "type": t, "value": value}


def act(*buttons: dict, half: bool = False) -> dict:
    """一行按钮。2个→bisected、3个→trisection 等宽并排；half=True 单按钮占左半。"""
    el = {"tag": "action", "actions": list(buttons)}
    if len(buttons) == 2 or half:
        el["layout"] = "bisected"
    elif len(buttons) == 3:
        el["layout"] = "trisection"
    return el


def _select_option(option: str | dict) -> dict:
    if isinstance(option, dict):
        val = str(option.get("value") or option.get("label") or "")
        label = str(option.get("label") or val)
    else:
        val = label = str(option)
    return {"text": {"tag": "plain_text", "content": label}, "value": val}


def select(placeholder: str, options: list[str | dict], initial: str | None, value: dict) -> dict:
    """下拉单选。options 可为字符串，或 {"label": 展示, "value": 回调值}。"""
    opts = [_select_option(o) for o in options]
    el = {
        "tag": "action",
        "actions": [{
            "tag": "select_static",
            "placeholder": {"tag": "plain_text", "content": placeholder},
            "options": opts,
            "value": value,
        }],
    }
    if initial and any(o["value"] == initial for o in opts):
        el["actions"][0]["initial_option"] = initial
    return el


def card(title: str | None, elements: list, template: str = "blue") -> dict:
    c = {"config": {"wide_screen_mode": True}, "elements": elements}
    if title:
        c["header"] = {"template": template, "title": {"tag": "plain_text", "content": title}}
    return c


def div(content: str) -> dict:
    """一段 lark_md 文本块。"""
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def plain_div(content: str) -> dict:
    return {"tag": "div", "text": {"tag": "plain_text", "content": content}}


def note(content: str) -> dict:
    """浅色小字备注，用于不打断主进度的状态提示。"""
    return {"tag": "note", "elements": [{"tag": "plain_text", "content": content}]}


def hr() -> dict:
    return {"tag": "hr"}


def _normalize_md_text(text: str) -> str:
    lines = []
    raw = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    i = 0
    while i < len(raw):
        line = raw[i]
        stripped = line.strip()
        if re.match(r"^#{1,6}\s+", stripped):
            title = re.sub(r"^#{1,6}\s+", "", stripped)
            lines.append(f"**{title}**")
        elif re.match(r"^[-*_]{3,}$", stripped):
            lines.append("\n---\n")
        elif re.match(r"^[-*+]\s+\[[ xX]\]\s+", line):
            checked = "[x]" in line.lower()
            item = re.sub(r"^[-*+]\s+\[[ xX]\]\s+", "", line).strip()
            lines.append(f"{'✓' if checked else '□'} {item}")
        elif re.match(r"^\s*[-*+]\s+", line):
            lines.append(re.sub(r"^\s*[-*+]\s+", "• ", line))
        elif _looks_like_table(raw, i):
            table, end = _consume_table(raw, i)
            lines.extend(_table_to_lines(table))
            i = end
            continue
        else:
            line = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"[图片：\1](\2)", line)
            lines.append(line)
        i += 1
    return "\n".join(lines).strip()


def _looks_like_table(lines: list[str], idx: int) -> bool:
    if idx + 1 >= len(lines):
        return False
    return "|" in lines[idx] and re.match(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$", lines[idx + 1])


def _consume_table(lines: list[str], idx: int) -> tuple[list[str], int]:
    out = [lines[idx], lines[idx + 1]]
    i = idx + 2
    while i < len(lines) and "|" in lines[i].strip():
        out.append(lines[i])
        i += 1
    return out, i


def _split_table_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _table_to_lines(table: list[str]) -> list[str]:
    headers = _split_table_row(table[0])
    rows = [_split_table_row(r) for r in table[2:]]
    out = []
    for row in rows:
        cells = []
        for idx, value in enumerate(row):
            label = headers[idx] if idx < len(headers) and headers[idx] else f"列{idx + 1}"
            cells.append(f"**{label}**：{value}")
        if cells:
            out.append("• " + "　".join(cells))
    return out or ["（表格无内容）"]


def _append_md_elements(elements: list, text: str) -> None:
    text = _normalize_md_text(text)
    if not text:
        return
    for block in re.split(r"\n\s*---\s*\n", text):
        block = block.strip()
        if block:
            for i in range(0, len(block), 2800):
                elements.append(div(block[i:i + 2800]))
        elements.append(hr())
    if elements and elements[-1].get("tag") == "hr":
        elements.pop()


def markdown_card(text: str) -> dict:
    elements = []
    pos = 0
    for m in re.finditer(r"```([A-Za-z0-9_+.-]*)\n([\s\S]*?)```", text):
        _append_md_elements(elements, text[pos:m.start()])
        lang = m.group(1).strip()
        code = m.group(2).rstrip()
        label = f"代码 {lang}" if lang else "代码"
        elements.append(div(f"**{label}**"))
        if code:
            for i in range(0, len(code), 2500):
                elements.append(plain_div(code[i:i + 2500]))
        pos = m.end()
    _append_md_elements(elements, text[pos:])
    return card(None, elements or [div("（空回复）")])


# ---------- 对话级通用卡 ----------
def codex_starting_card(elapsed: int = 0, supplement: str = "") -> dict:
    elements = [
        div(
            f"🚀 **正在启动 Codex**　·　⏱ 已用 {elapsed}s\n"
            "当前对话的 Codex 会话不存在或已退出，启动完成后会自动开始处理。"
        ),
    ]
    if supplement:
        elements.append(note(supplement))
    return card(None, elements)


def thinking_card(activity: str, elapsed: int, supplement: str = "") -> dict:
    elements = [div(f"🔄 **执行中**　·　⏱ 已用 {elapsed}s\n{activity}")]
    if supplement:
        elements.append(note(supplement))
    else:
        elements.append(act(btn("⏹ 停止", {"action": "stop"}, "danger")))
    return card(None, elements)


def stopped_card(activity: str = "", elapsed: int | None = None) -> dict:
    lines = ["⏹ **已中断当前任务**"]
    if activity:
        lines.append(f"中断前步骤：{activity}")
    if elapsed is not None:
        lines.append(f"已运行 {elapsed}s")
    lines.append("可以继续发送新消息开始下一轮。")
    return card(None, [div("\n".join(lines))])


def answer_card(text: str) -> dict:
    if len(text) > 3500:
        text = text[:3500] + "\n\n…（内容过长已截断）"
    return card(None, [div(text)])


def text_card(text: str) -> dict:
    """纯文本卡（plain_text，不渲染 Markdown，原样显示）。"""
    return card(None, [{"tag": "div", "text": {"tag": "plain_text", "content": text}}])
