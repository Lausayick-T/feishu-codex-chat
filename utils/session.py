"""
session.py —— tmux 持续化 Codex 会话管理。

每个飞书会话(chat_id) 对应一个常驻 tmux session，里面跑一个交互式 agent
消息通过 bracketed paste 注入，回复通过 Codex session 文件 tail 捕获。
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import chatconfig, config, hub

ROOT = Path(__file__).resolve().parent.parent  # chat-agent/
_CONFIG = config.load()
_ENGINES = _CONFIG.get("engines", {})
_CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
_CODEX_SESSION_CACHE: dict[str, Path] = {}


def _session_day_dirs(root: Path, after: float | None) -> list[Path]:
    if after is None:
        return sorted((p for p in root.glob("*/*/*") if p.is_dir()), reverse=True)
    dates = set()
    for base in (datetime.fromtimestamp(after), datetime.fromtimestamp(after, timezone.utc)):
        for offset in (-1, 0, 1):
            dates.add((base + timedelta(days=offset)).date())
    return [
        path
        for date in sorted(dates, reverse=True)
        if (path := root / f"{date.year:04d}" / f"{date.month:02d}" / f"{date.day:02d}").is_dir()
    ]


def _session_matches(path: Path, target: str) -> bool:
    try:
        with path.open(encoding="utf-8") as stream:
            first = json.loads(stream.readline())
    except Exception:
        return False
    return first.get("type") == "session_meta" and (first.get("payload") or {}).get("cwd") == target


def latest_codex_session(
    workdir: Path, *, after: float | None = None, exclude: Path | None = None
) -> Path | None:
    """按 cwd 找最新 Codex rollout；缓存常规查询，启动时只扫描最近日期目录。"""
    root = _CODEX_SESSIONS_DIR
    if not root.is_dir():
        return None
    target = str(workdir.resolve())
    cached = _CODEX_SESSION_CACHE.get(target)
    if cached and cached != exclude and cached.exists():
        try:
            if after is None or cached.stat().st_mtime >= after:
                return cached
        except OSError:
            pass
    for day in _session_day_dirs(root, after):
        files = []
        for path in day.glob("rollout-*.jsonl"):
            if path == exclude:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if after is None or mtime >= after:
                files.append((mtime, path))
        for _, path in sorted(files, key=lambda item: item[0], reverse=True):
            if _session_matches(path, target):
                _CODEX_SESSION_CACHE[target] = path
                return path
    return None


# ---------- 启动命令 ----------
def build_command(workdir: Path) -> str:
    """按该工作目录的 Codex 模型和 reasoning effort 配置生成启动命令。"""
    cfg = chatconfig.get(workdir)
    codex_model = cfg.get("codex_model", chatconfig.DEFAULTS["codex_model"])
    codex_effort = cfg.get("codex_effort", chatconfig.DEFAULTS["codex_effort"])
    ecfg = _ENGINES.get("codex", {})
    cmd = ecfg.get(
        "command",
        "codex --model {codex_model} --dangerously-bypass-approvals-and-sandbox --dangerously-bypass-hook-trust",
    ).format(
        codex_model=shlex.quote(str(codex_model)),
        workdir=shlex.quote(str(workdir.resolve())),
        root=shlex.quote(str(ROOT)),
    )
    return f"{cmd} -c model_reasoning_effort={shlex.quote(str(codex_effort))}"


# ---------- tmux 基础 ----------
SESSION_PREFIX = "feishu-"


def safe_chat_id(chat_id: str) -> str:
    """把飞书 chat_id 转成可安全用于目录和 tmux 的标识。"""
    return re.sub(r"[^A-Za-z0-9_-]", "_", chat_id)


def session_name(chat_id: str) -> str:
    return f"{SESSION_PREFIX}{safe_chat_id(chat_id)}"


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def exists(name: str) -> bool:
    return _tmux("has-session", "-t", name).returncode == 0


def _spawn(chat_id: str, workdir: Path) -> None:
    """新建 tmux 会话并按配置启动引擎（不等待就绪）。"""
    name = session_name(chat_id)
    workdir.mkdir(parents=True, exist_ok=True)
    hub.apply_defaults(workdir)  # 启动前确保默认必装的 skill/mcp 在位
    cmd = build_command(workdir)
    _tmux("new-session", "-d", "-s", name, "-c", str(workdir), "-x", "220", "-y", "50")
    # 注入 CHAT_ID + CHAT_AGENT_HOME（agent 用它找 hub_cli.py）
    launch = f"export CHAT_ID={chat_id}; export CHAT_AGENT_HOME={ROOT}; {cmd}"
    _tmux("send-keys", "-t", name, "-l", "--", launch)
    _tmux("send-keys", "-t", name, "Enter")


_CODEX_HEADER_RE = re.compile(r">_\s+OpenAI Codex(?:\s+\(v[^)]+\))?", re.I)
_CODEX_PROMPT_RE = re.compile(r"^\s*›(?:\s|$)", re.M)
_CODEX_MCP_FAILURE_RE = re.compile(r"MCP startup incomplete\s*\(failed:\s*([^)]+)\)", re.I)
_CODEX_BOOT_ENTER_INTERVAL = 1.5
_CODEX_READY_SETTLE_SEC = 2.0


_SHELL_CMDS = {"zsh", "-zsh", "bash", "-bash", "sh", "-sh", "fish", "tcsh", "login"}


def _agent_alive(name: str) -> bool:
    """会话里跑的是不是 Codex 而非已回落到 shell。"""
    cmds = _tmux("list-panes", "-t", name, "-F", "#{pane_current_command}").stdout.split()
    return any(c not in _SHELL_CMDS for c in cmds) if cmds else False


def _codex_process_alive(chat_id: str) -> bool:
    rows = _tmux(
        "list-panes", "-t", session_name(chat_id), "-F", "#{pane_dead}\t#{pane_current_command}"
    ).stdout.splitlines()
    for row in rows:
        dead, _, command = row.partition("\t")
        if dead == "0" and command.lower().startswith("codex"):
            return True
    return False


def _codex_pane_text(chat_id: str) -> str:
    return _tmux("capture-pane", "-p", "-t", session_name(chat_id), "-S", "-400").stdout


def interrupt(chat_id: str) -> None:
    """打断当前轮（发 ESC）。"""
    name = session_name(chat_id)
    _tmux("send-keys", "-t", name, "Escape")
    time.sleep(0.2)
    _tmux("send-keys", "-t", name, "Escape")


def ensure(
    chat_id: str,
    workdir: Path,
    *,
    cold_wait: float,
    on_start: Callable[[], None] | None = None,
) -> bool:
    """确保 chat_id 的会话在跑。返回 True 表示这次是冷启动。

    仅在确定需要新建 tmux/Codex 时调用 ``on_start``，供上层在阻塞等待
    TUI 就绪前立即向用户展示冷启动状态。
    """
    name = session_name(chat_id)
    if exists(name):
        if _agent_alive(name):
            return False
        kill(chat_id)  # 壳还在但 agent 死了 → 重起
        time.sleep(0.5)
    if on_start is not None:
        on_start()
    before_codex = latest_codex_session(workdir)
    spawn_at = time.time()
    _spawn(chat_id, workdir)
    if not _wait_codex_ready(chat_id, workdir, before_codex, timeout=max(cold_wait, 45.0), after=spawn_at - 1):
        time.sleep(cold_wait)
    return True


def _codex_tui_ready(chat_id: str) -> bool:
    out = _codex_pane_text(chat_id)
    return bool(_CODEX_HEADER_RE.search(out) and _CODEX_PROMPT_RE.search(out) and _codex_process_alive(chat_id))


def codex_tui_ready(chat_id: str) -> bool:
    """当前 Codex 已回到可输入状态。供回复 worker 判断是否需要补偿提取终稿。"""
    return _codex_tui_ready(chat_id)


def codex_startup_warning(chat_id: str) -> str:
    """Codex 可对话但 MCP 启动降级时，返回失败的 MCP 名称。"""
    match = _CODEX_MCP_FAILURE_RE.search(_codex_pane_text(chat_id))
    return match.group(1).strip() if match else ""


def _wait_codex_ready(chat_id: str, workdir: Path, before, timeout: float, *, after: float | None = None) -> bool:
    """推进启动提示，直到 Codex 进程、版本标题和输入提示符均已就绪。

    新 rollout 可能要到第一条用户消息后才落盘，因此不再把它作为启动成功的硬条件。
    """
    start = time.time()
    last_enter = 0.0
    ready_since: float | None = None
    settle = min(_CODEX_READY_SETTLE_SEC, max(0.1, timeout / 3))
    while time.time() - start < timeout:
        pane = _codex_pane_text(chat_id)
        header_seen = bool(_CODEX_HEADER_RE.search(pane))
        prompt_seen = bool(_CODEX_PROMPT_RE.search(pane))
        process_alive = _codex_process_alive(chat_id)
        ready = process_alive and header_seen and prompt_seen
        if ready:
            ready_since = ready_since or time.time()
            # TUI 会先画出输入框，MCP 的启动失败可能随后才打印。等待短暂稳定窗口，
            # 让调用方紧接着读取 codex_startup_warning 时不会漏报降级。
            if time.time() - ready_since >= settle:
                return True
        else:
            ready_since = None
        # Codex 启动前可能停在更新、信任目录、登录提示等确认页；标题出现后停止自动回车。
        if not header_seen and process_alive and time.time() - last_enter >= _CODEX_BOOT_ENTER_INTERVAL:
            _tmux("send-keys", "-t", session_name(chat_id), "Enter")
            last_enter = time.time()
        time.sleep(min(0.5, settle / 2))
    return False


def restart(chat_id: str, workdir: Path) -> bool:
    """重启会话内的引擎（杀旧建新），让新的引擎/模型/MCP/配置生效。"""
    if exists(session_name(chat_id)):
        kill(chat_id)
        time.sleep(0.5)
    before_codex = latest_codex_session(workdir)
    spawn_at = time.time()
    _spawn(chat_id, workdir)
    return _wait_codex_ready(chat_id, workdir, before_codex, timeout=45.0, after=spawn_at - 1)


def send_command(chat_id: str, command: str) -> None:
    """注入一条 slash 命令并回车（如 /clear、/status）。

    先发 Ctrl-U 清空当前输入行，避免命令被追加到未提交文本后面，导致 TUI
    把它当成普通多行消息的一部分。
    """
    name = session_name(chat_id)
    _tmux("send-keys", "-t", name, "C-u")
    time.sleep(0.1)
    _tmux("send-keys", "-t", name, "-l", "--", command)
    time.sleep(0.1)
    _tmux("send-keys", "-t", name, "Enter")
    time.sleep(0.3)


def send(chat_id: str, text: str) -> None:
    """把一段文本注入输入框并回车（bracketed paste，支持多行）。"""
    name = session_name(chat_id)
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write(text)
        tmp = f.name
    try:
        _tmux("load-buffer", "-b", "ccbuf", tmp)
        _tmux("paste-buffer", "-p", "-b", "ccbuf", "-t", name, "-d")
        time.sleep(0.4)
        _tmux("send-keys", "-t", name, "Enter")
    finally:
        Path(tmp).unlink(missing_ok=True)


def kill(chat_id: str) -> None:
    _tmux("kill-session", "-t", session_name(chat_id))


def capture(chat_id: str, lines: int = 40) -> str:
    res = _tmux("capture-pane", "-p", "-t", session_name(chat_id), "-S", f"-{lines}")
    return res.stdout
