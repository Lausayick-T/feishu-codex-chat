"""
chatconfig.py —— 每个会话(工作目录)的功能配置读写。

配置文件放在该会话的工作目录下：bots/<dir>/agent.json
目前支持：
  memory_mode: "resume"（依赖当前常驻 Codex TUI 的完整上下文）
             | "fresh"（额外维护 memory/ 摘要，跨会话重启可恢复）
"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULTS = {
    "memory_mode": "fresh",
    # 对话引擎固定为 Codex；engine 仅保留为内部状态，用户侧不提供引擎选择。
    "engine": "codex",
    "codex_model": "gpt-5.5",
    "codex_effort": "high",
    "codex_effective_model": "",
    # MCP 参数/版本覆盖：{ "<hub mcp名>": { "<serverKey>": {"env":{...},"args":[...],...} } }
    # 装载时与 hub/mcp/<名>.json 深合并，覆盖项胜出。每群独立。
    "mcp_overrides": {},
    # 过程返回：只原地刷新当前状态，最终卡片只显示最终输出
    "progress_mode": "compact",
    # 0 表示不自动清理；记忆和 workspace 始终只允许手动清理。
    "attachment_retention_days": 30,
    "task_log_retention_days": 30,
}

# 各配置项的中文标签（UI 显示用）
MODE_LABEL = {"resume": "当前会话上下文", "fresh": "摘要记忆 (memory/)"}
OBSOLETE_KEYS = {"reply_mode", "reply_format"}
CODEX_MODELS = {
    "gpt-5.6-sol": "GPT-5.6 Sol",
    "gpt-5.6-terra": "GPT-5.6 Terra",
    "gpt-5.6-luna": "GPT-5.6 Luna",
    "gpt-5.5": "GPT-5.5",
    "gpt-5.4-mini": "GPT-5.4 Mini",
    "gpt-5-codex": "GPT-5 Codex",
    "gpt-5": "GPT-5",
}
CODEX_EFFORT_LABELS = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "Extra High",
    "max": "Max",
    "ultra": "Ultra",
}
_BASE_CODEX_EFFORTS = ("low", "medium", "high", "xhigh")
CODEX_MODEL_EFFORTS = {
    "gpt-5.6-sol": (*_BASE_CODEX_EFFORTS, "max", "ultra"),
    "gpt-5.6-terra": (*_BASE_CODEX_EFFORTS, "max", "ultra"),
    "gpt-5.6-luna": (*_BASE_CODEX_EFFORTS, "max"),
    "gpt-5.5": _BASE_CODEX_EFFORTS,
    "gpt-5.4-mini": _BASE_CODEX_EFFORTS,
    "gpt-5-codex": _BASE_CODEX_EFFORTS,
    "gpt-5": _BASE_CODEX_EFFORTS,
}
def codex_efforts_for(model: str) -> tuple[str, ...]:
    return CODEX_MODEL_EFFORTS.get(model, _BASE_CODEX_EFFORTS)


def normalize_codex_effort(model: str, effort: str) -> str:
    supported = codex_efforts_for(model)
    return effort if effort in supported else ("medium" if "medium" in supported else supported[0])


def engine_model_label(cfg: dict) -> str:
    model = cfg.get("codex_effective_model") or cfg.get("codex_model", DEFAULTS["codex_model"])
    label = "Codex · " + CODEX_MODELS.get(model, model)
    requested = cfg.get("codex_model", DEFAULTS["codex_model"])
    if cfg.get("codex_effective_model") and model != requested:
        label += f"（配置 {CODEX_MODELS.get(requested, requested)}）"
    effort = normalize_codex_effort(model, cfg.get("codex_effort", DEFAULTS["codex_effort"]))
    return f"{label} · {CODEX_EFFORT_LABELS.get(effort, effort)}"

# fresh 模式写入工作目录的 AGENTS.md，用这个标记认领（切回 resume 时据此删除）
FRESH_MARKER = "<!-- chat-agent:fresh -->"

FRESH_AGENTS_MD = FRESH_MARKER + """
# 记忆模式：fresh + memory/

当前会话可能保留临时上下文，但会话重启后不会自动恢复完整聊天。本目录的 `memory/` 是跨重启的长期记忆来源。

规则：
1. 回答前**先读 `memory/MEMORY.md` 和 `memory/history_chat.md`**，把里面内容当作已知背景。
2. `memory/MEMORY.md` 是长期稳定记忆主索引：用户偏好、重要事实、长期目标、稳定约束；它也可以索引 `memory/` 下的其他专题文件。
3. `memory/history_chat.md` 放历史对话的滚动摘要：近期讨论、阶段性结论、待跟进事项。不要存逐字聊天记录。
4. 回答后按需精炼更新这两个文件；能进 `memory/MEMORY.md` 的内容必须足够稳定，普通过程信息写进 `memory/history_chat.md`。
5. 两个文件不存在就创建它们。
6. 对话中生成的报告、脚本、数据、图片、导出文件和临时产物统一写入 `workspace/`，不要散落在会话根目录；`memory/` 只放记忆，`incoming/` 只放用户上传附件。
"""

FRESH_MEMORY_MD = "# 长期稳定记忆\n\n（主索引：用户偏好、重要事实、长期目标、稳定约束；可索引 `memory/` 下其他专题文件。）\n"
FRESH_HISTORY_CHAT_MD = "# 对话滚动摘要\n\n（近期讨论、阶段性结论、待跟进事项。不要存逐字聊天记录。）\n"
LEGACY_MEMORY_NOTE = (
    "# 旧记忆文件\n\n"
    "当前记忆已迁移到 `memory/MEMORY.md` 和 `memory/history_chat.md`。\n"
    "请优先读取并维护 `memory/` 目录下的文件。\n"
)
OLD_LEGACY_MEMORY_NOTE = (
    "# 旧记忆文件\n\n"
    "当前记忆已迁移到 `memory/core.md` 和 `memory/history.md`。\n"
    "请优先读取并维护 `memory/` 目录下的文件。"
)


def _generated_memory_text(text: str) -> bool:
    return text.strip() in {
        LEGACY_MEMORY_NOTE.strip(),
        OLD_LEGACY_MEMORY_NOTE.strip(),
        FRESH_MEMORY_MD.strip(),
        FRESH_HISTORY_CHAT_MD.strip(),
        "# 核心记忆\n\n（长期稳定信息：用户偏好、重要事实、长期目标、稳定约束。）",
        "# 对话记忆\n\n（滚动摘要：近期讨论、阶段性结论、待跟进事项。不要存逐字聊天记录。）",
    }


def _read_migration_source(path: Path) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    if "当前记忆已迁移到 `memory/core.md` 和 `memory/history.md`" in text:
        return ""
    return "" if not text or _generated_memory_text(text) else text


def _ensure_memory_files(workdir: Path) -> None:
    memory_dir = workdir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    memory_md = memory_dir / "MEMORY.md"
    history_chat_md = memory_dir / "history_chat.md"
    legacy_root = workdir / "MEMORY.md"
    legacy_core = memory_dir / "core.md"
    legacy_history = memory_dir / "history.md"

    if not memory_md.exists():
        content = FRESH_MEMORY_MD
        migrated = []
        for label, path in (("旧 MEMORY.md", legacy_root), ("旧 memory/core.md", legacy_core)):
            text = _read_migration_source(path)
            if text:
                migrated.append(f"## 从 {label} 迁移\n\n{text}")
        if migrated:
            content += "\n" + "\n\n".join(migrated) + "\n"
        memory_md.write_text(content, encoding="utf-8")

    if not history_chat_md.exists():
        content = FRESH_HISTORY_CHAT_MD
        text = _read_migration_source(legacy_history)
        if text:
            content += "\n## 从旧 memory/history.md 迁移\n\n" + text + "\n"
        history_chat_md.write_text(content, encoding="utf-8")

    if legacy_root.exists():
        text = legacy_root.read_text(encoding="utf-8")
        if text != LEGACY_MEMORY_NOTE:
            legacy_root.write_text(LEGACY_MEMORY_NOTE, encoding="utf-8")


def _ensure_workspace(workdir: Path) -> None:
    (workdir / "workspace").mkdir(parents=True, exist_ok=True)


def _path(workdir: Path) -> Path:
    return workdir / "agent.json"


def get(workdir: Path) -> dict:
    """读取该工作目录的配置，缺省项用 DEFAULTS 补齐。"""
    cfg = dict(DEFAULTS)
    p = _path(workdir)
    if p.exists():
        try:
            stored = json.loads(p.read_text(encoding="utf-8"))
            cfg.update(stored)
        except json.JSONDecodeError:
            pass
    if cfg.get("engine") != "codex":
        cfg["engine"] = "codex"
    cfg["codex_effort"] = normalize_codex_effort(
        cfg.get("codex_model", DEFAULTS["codex_model"]),
        cfg.get("codex_effort", DEFAULTS["codex_effort"]),
    )
    if cfg.get("progress_mode") != "compact":
        cfg["progress_mode"] = "compact"
    for key in OBSOLETE_KEYS:
        cfg.pop(key, None)
    return cfg


def set_value(workdir: Path, key: str, value) -> dict:
    """更新一项配置并落盘，返回更新后的完整配置。"""
    return set_values(workdir, {key: value})


def set_values(workdir: Path, values: dict) -> dict:
    """原子更新多项配置并落盘，返回更新后的完整配置。"""
    cfg = get(workdir)
    for key, value in values.items():
        if key in OBSOLETE_KEYS:
            continue
        cfg[key] = "codex" if key == "engine" else value
    cfg["codex_effort"] = normalize_codex_effort(
        cfg.get("codex_model", DEFAULTS["codex_model"]),
        cfg.get("codex_effort", DEFAULTS["codex_effort"]),
    )
    workdir.mkdir(parents=True, exist_ok=True)
    _path(workdir).write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def init_defaults(workdir: Path) -> dict:
    """为新工作目录写入默认 agent.json，并应用默认记忆模式脚手架。"""
    workdir.mkdir(parents=True, exist_ok=True)
    _ensure_workspace(workdir)
    p = _path(workdir)
    if p.exists():
        cfg = get(workdir)
        p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        cfg = dict(DEFAULTS)
        p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    apply_memory_scaffold(workdir, cfg.get("memory_mode", DEFAULTS["memory_mode"]))
    return cfg


def apply_memory_scaffold(workdir: Path, mode: str) -> None:
    """根据记忆模式维护工作目录里的 AGENTS.md / memory/。"""
    agents_md = workdir / "AGENTS.md"
    _ensure_workspace(workdir)
    _ensure_memory_files(workdir)
    if mode == "fresh":
        agents_md.write_text(FRESH_AGENTS_MD, encoding="utf-8")
    else:  # resume：删掉我们之前为 fresh 写的指令文件（避免误导），保留 memory/
        if agents_md.exists() and FRESH_MARKER in agents_md.read_text(encoding="utf-8"):
            agents_md.unlink()


def switch_memory(workdir: Path, mode: str) -> str:
    """切换记忆模式并维护脚手架，返回结论文本。"""
    set_value(workdir, "memory_mode", mode)
    apply_memory_scaffold(workdir, mode)
    return f"✅ 已切换为 {MODE_LABEL.get(mode, mode)}"
