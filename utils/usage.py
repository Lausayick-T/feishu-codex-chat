"""usage.py —— Codex `/status` 用量/状态输出解析。

剥掉 TUI 边框 / 进度条方块，只保留模型 / 账号 / 5h limit / weekly limit；其余（context window、
会话 ID、版本等）视为噪音丢弃。

对外函数：
  - parse_codex_status(text) -> dict           Codex `/status` → {"model": str, "lines": [...]}
      · model 供上层记录 codex_effective_model；lines 直接用于展示
"""

from __future__ import annotations

import re

# TUI 边框、列表符号（capture-pane -p 已去掉 ANSI 颜色，留下这些线框字符）
_DECOR = "│╭╰╮╯─┃┏┓┗┛━┈┊║╔╗╚╝·•*"
# 进度条：方块条（████░░░）或括号条（[####----]）一律去掉，只留后面的百分比文字
_BAR = re.compile(r"\[[\s#=.:|·\-█▓▒░▐▌▏▎▍▋▊▉]*\]|[█▓▒░▐▌▏▎▍▋▊▉]{2,}")
# 覆盖层的操作提示 / 页脚，一律丢弃
_DROP = re.compile(r"\besc\b|to close|to exit|to select|to send|↑|↓|←|→|press\s|shortcuts", re.I)

_KEEP_CODEX = re.compile(r":|%|resets?|limit|left|used|remaining", re.I)

# 噪音：只想留账户/模型/限额，其余字段一律丢弃。
_NOISE = re.compile(
    r"reasoning|effort|summar|token|input|output|\btotal\b|context\s*window|context\s*left|"
    r"session\s*id|client|\bcli\b|version|\bplan\b|cost|cache",
    re.I,
)

# codex 用量卡只展示这些字段，并按该顺序输出。
_CODEX_LABEL = {
    "model": "模型",
    "account": "账号",
    "5h_limit": "5h limit",
    "weekly_limit": "weekly limit",
}
_CODEX_ORDER = ("model", "account", "5h_limit", "weekly_limit")


_PCT = re.compile(r"(\d{1,3})\s*%")


def _bar(pct: int, width: int = 10) -> str:
    """按百分比渲染统一风格进度条（现代细条）：▰ 已用 / ▱ 剩余。"""
    pct = max(0, min(100, pct))
    filled = int(pct / 100 * width + 0.5)
    return "▰" * filled + "▱" * (width - filled)


def _with_bar(line: str) -> str:
    """行内出现「NN%」时，在行尾补一条统一风格进度条；否则原样返回。"""
    m = _PCT.search(line)
    if not m:
        return line
    return f"{line}　{_bar(int(m.group(1)))}"


def _reset_text(value: str) -> str:
    """Extract reset text as: " (reset 01:33 on 8 Jul)"."""
    m = re.search(r"\((?:resets?|resetting)\s+([^)]+)\)", value, re.I)
    if not m:
        m = re.search(r"\b(?:resets?|resetting)\s+(?:at\s+)?([^,;]+)", value, re.I)
    if not m:
        return ""
    reset = re.sub(r"\s+", " ", m.group(1)).strip()
    return f" (reset {reset})" if reset else ""


def _left_pct(value: str) -> int | None:
    m = re.search(r"(\d{1,3})\s*%\s*(?:left|remaining)", value, re.I)
    if m:
        return max(0, min(100, int(m.group(1))))
    m = re.search(r"(\d{1,3})\s*%\s*used", value, re.I)
    if m:
        return max(0, min(100, 100 - int(m.group(1))))
    m = _PCT.search(value)
    if m:
        return max(0, min(100, int(m.group(1))))
    return None


def _limit_block(label: str, value: str) -> str:
    """Format limit rows as a title line plus a left-percent bar line."""
    reset = _reset_text(value)
    pct = _left_pct(value)
    if pct is None:
        body = re.sub(r"\s*\((?:resets?|resetting)[^)]+\)", "", value, flags=re.I).strip()
        return f"{label}{reset}" + (f"\n{body}" if body else "")
    return f"{label}{reset}\n{pct}% left　{_bar(pct)}"


def _clean(line: str) -> str:
    line = re.sub(rf"^[\s{re.escape(_DECOR)}]+", "", line)
    line = re.sub(rf"[\s{re.escape(_DECOR)}]+$", "", line)
    line = _BAR.sub(" ", line)
    return re.sub(r"[ \t]+", " ", line).strip()


def _extract(text: str, keep: re.Pattern) -> list[str]:
    """通用抽取：清洗每行 → 丢页脚 → 把「(resets …)」等续行并入上一行 → 命中 keep 才保留。"""
    out: list[str] = []
    for raw in text.splitlines():
        line = _clean(raw)
        if not line or _DROP.search(line):
            continue
        # 续行（重置时间/补充说明）并入上一条，避免拆散信息
        if line.startswith("(") and out:
            out[-1] = f"{out[-1]} {line}".strip()
            continue
        if keep.search(line) and (not out or out[-1] != line):
            out.append(line)
    return out


def parse_codex_status(text: str) -> dict:
    """codex `/status` 抓屏 → {"model": 检测到的模型或"", "lines": 展示行}。"""
    model = ""
    found: dict[str, str] = {}
    for line in _extract(text, _KEEP_CODEX):
        if ":" in line:
            label, value = (x.strip() for x in line.split(":", 1))
            if not value or _NOISE.search(label):  # 按字段名过滤噪音
                continue
            key = _codex_key(label)
            if not key:
                continue
            if key == "model":
                if not model:
                    model = value
                value = value.split("(", 1)[0].strip()  # 去掉 (reasoning …) 之类附注
            if key in {"5h_limit", "weekly_limit"}:
                found[key] = _limit_block(_CODEX_LABEL[key], value)
            else:
                found[key] = f"{_CODEX_LABEL[key]}：{value}"
        else:
            key = _codex_key(line)
            if key:
                found[key] = _with_bar(line)
    lines = [found[key] for key in _CODEX_ORDER if key in found]
    return {"model": model, "lines": lines}


def _codex_key(label: str) -> str:
    normalized = re.sub(r"[\s_-]+", " ", label.strip().lower())
    if normalized == "model":
        return "model"
    if normalized in {"account", "signed in", "logged in", "login"} or "@" in normalized:
        return "account"
    if re.search(r"\b5\s*(h|hour)", normalized) and "limit" in normalized:
        return "5h_limit"
    if "week" in normalized and "limit" in normalized:
        return "weekly_limit"
    return ""
