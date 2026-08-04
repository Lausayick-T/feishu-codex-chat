"""
hub.py —— skill / mcp 仓库的装载逻辑（纯逻辑，文件系统即真相源）。

仓库结构（本地，可在 config 里改路径）：
  hub/skills/<名>/SKILL.md(+附带文件)   一个目录 = 一个 skill
  hub/mcp/<名>.json                      外部 MCP 的纯配置
  hub/mcp/<名>/mcp.json                  Codex 生成的 MCP bundle（可带源码/依赖/测试）
  hub/defaults.json                      {"skills":[...], "mcp":[...]} 每个任务必装项

装载到某会话工作目录 bots/<群>/：
  skill → 复制到  <wd>/.codex/skills/<名>/
  bundle MCP → 复制到 <wd>/.codex/mcp/<名>/，${MCP_BUNDLE_DIR} 会解析为该目录
  mcp   → 合并进  <wd>/.mcp.json 的 mcpServers 和 <wd>/.codex/config.toml 的 mcp_servers
Skill 由 Codex 自动检测变更；MCP 配置在当前 Codex 会话重启后重新初始化。
"""

from __future__ import annotations

import json
import hashlib
import shutil
import tempfile
import time
from pathlib import Path

from . import chatconfig, config
from .resource_lifecycle import (
    MCP_BUNDLE_TOKEN,
    ValidationReport,
    load_mcp_document,
    scaffold_mcp,
    scaffold_skill,
    validate_mcp_definition,
    validate_name,
    validate_skill_dir,
)

ROOT = Path(__file__).resolve().parent.parent
HUB = ROOT / "hub"
SKILLS = HUB / "skills"
MCP = HUB / "mcp"
DEFAULT_STATE = ".chat_agent_system_defaults.json"
RESOURCE_STATE = ".chat_agent_hub_resources.json"
RESOURCE_UPDATE_NOTICE = ".chat_agent_resource_update.json"
LEGACY_RESTART_REQUEST = ".chat_agent_restart_request.json"


# ---------- 仓库清单 ----------
def list_skills() -> list[str]:
    if not SKILLS.is_dir():
        return []
    return sorted(p.name for p in SKILLS.iterdir() if p.is_dir())


def list_mcp() -> list[str]:
    if not MCP.is_dir():
        return []
    names = {p.stem for p in MCP.glob("*.json")}
    names.update(p.name for p in MCP.iterdir() if p.is_dir() and (p / "mcp.json").is_file())
    return sorted(names)


def defaults() -> dict:
    p = HUB / "defaults.json"
    if not p.exists():
        return {"skills": [], "mcp": []}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"skills": [], "mcp": []}
    return {"skills": d.get("skills", []), "mcp": d.get("mcp", [])}


def set_system_default(kind: str, name: str, *, enabled: bool = True) -> bool:
    """Promote/demote an existing general resource to/from new-session defaults."""
    safe_name = _require_resource_name(name)
    if kind not in {"skill", "mcp"}:
        raise ValueError("kind 必须是 skill 或 mcp")
    available = list_skills() if kind == "skill" else list_mcp()
    if safe_name not in available:
        raise ValueError(f"Hub 中不存在 {kind} {safe_name}")
    data = defaults()
    key = "skills" if kind == "skill" else "mcp"
    values = list(dict.fromkeys(str(item) for item in data[key]))
    changed = False
    if enabled and safe_name not in values:
        values.append(safe_name)
        changed = True
    elif not enabled and safe_name in values:
        values.remove(safe_name)
        changed = True
    data[key] = sorted(values)
    if changed:
        path = HUB / "defaults.json"
        staged = path.with_suffix(".json.tmp")
        staged.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        staged.replace(path)
    return changed


def _default_state_path(workdir: Path) -> Path:
    return workdir / DEFAULT_STATE


def _read_default_state(workdir: Path) -> dict:
    p = _default_state_path(workdir)
    if not p.exists():
        return {"skills": [], "mcp": []}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"skills": [], "mcp": []}
    return {"skills": d.get("skills", []), "mcp": d.get("mcp", [])}


def _write_default_state(workdir: Path, data: dict) -> None:
    p = _default_state_path(workdir)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _resource_state_path(workdir: Path) -> Path:
    return workdir / RESOURCE_STATE


def _read_resource_state(workdir: Path) -> dict:
    path = _resource_state_path(workdir)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("skills", {})
    data.setdefault("mcp", {})
    return data


def _write_resource_state(workdir: Path, data: dict) -> None:
    path = _resource_state_path(workdir)
    staged = path.with_suffix(path.suffix + ".tmp")
    staged.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    staged.replace(path)


def _path_digest(path: Path) -> str:
    digest = hashlib.sha256()
    if path.is_file():
        digest.update(path.read_bytes())
        return digest.hexdigest()
    if not path.is_dir():
        return ""
    for item in sorted((p for p in path.rglob("*") if p.is_file()), key=lambda p: p.relative_to(path).as_posix()):
        digest.update(item.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(item.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def resource_revision(kind: str, name: str) -> dict:
    """返回 Hub 资源的自动内容版本；有 SemVer 时同时保留。"""
    safe_name = _require_resource_name(name)
    if kind == "skill":
        source = SKILLS / safe_name
        version_path = source / "VERSION"
    elif kind == "mcp":
        source = _hub_mcp_path(safe_name)
        version_path = _hub_mcp_bundle(safe_name) / "VERSION"
    else:
        raise ValueError("kind 必须是 skill 或 mcp")
    version = ""
    if version_path.is_file():
        version = version_path.read_text(encoding="utf-8").strip().splitlines()[0]
    digest = _path_digest(source)
    return {
        "version": version,
        "digest": digest,
        "revision": version or (f"rev-{digest[:12]}" if digest else ""),
    }


def _record_resource(workdir: Path, kind: str, name: str, *, keys: list[str] | None = None) -> None:
    state = _read_resource_state(workdir)
    entry = resource_revision(kind, name)
    if keys is not None:
        entry["keys"] = sorted(keys)
    state["skills" if kind == "skill" else "mcp"][name] = entry
    _write_resource_state(workdir, state)


def _forget_resource(workdir: Path, kind: str, name: str) -> dict:
    state = _read_resource_state(workdir)
    entry = state["skills" if kind == "skill" else "mcp"].pop(name, {})
    _write_resource_state(workdir, state)
    return entry if isinstance(entry, dict) else {}


def _resource_update_path(workdir: Path) -> Path:
    return workdir / RESOURCE_UPDATE_NOTICE


def pending_resource_updates(workdir: Path) -> dict:
    """Return resource updates waiting to be acknowledged/applied in the control panel."""
    path = _resource_update_path(workdir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "skills": sorted({str(item) for item in payload.get("skills", []) if str(item)}),
        "mcp": sorted({str(item) for item in payload.get("mcp", []) if str(item)}),
        "updated_at": payload.get("updated_at"),
    }


def mark_resource_updates(workdir: Path, changes: list[str]) -> Path | None:
    """Record updates for panel display without granting the conversation restart authority."""
    pending = pending_resource_updates(workdir)
    skills = set(pending["skills"])
    mcp = set(pending["mcp"])
    for change in changes:
        parts = str(change).split(":")
        if len(parts) < 2:
            continue
        kind, name = parts[0], parts[1]
        label = f"{name}（已移除）" if len(parts) > 2 and parts[2] == "removed" else name
        if kind == "skill":
            skills.add(label)
        elif kind == "mcp":
            mcp.add(label)
    if not skills and not mcp:
        return None
    path = _resource_update_path(workdir)
    staged = path.with_suffix(path.suffix + ".tmp")
    staged.write_text(
        json.dumps(
            {"skills": sorted(skills), "mcp": sorted(mcp), "updated_at": time.time()},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    staged.replace(path)
    return path


def dismiss_skill_updates(workdir: Path) -> None:
    pending = pending_resource_updates(workdir)
    if pending["mcp"]:
        mark = {
            "skills": [],
            "mcp": pending["mcp"],
            "updated_at": pending["updated_at"] or time.time(),
        }
        path = _resource_update_path(workdir)
        path.write_text(json.dumps(mark, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    else:
        _resource_update_path(workdir).unlink(missing_ok=True)


def clear_resource_updates(workdir: Path) -> None:
    """Clear panel notices after the user explicitly restarts the current Codex session."""
    _resource_update_path(workdir).unlink(missing_ok=True)
    # Old versions let a conversation stage an automatic restart. Never execute stale requests.
    (workdir / LEGACY_RESTART_REQUEST).unlink(missing_ok=True)


# ---------- 工作目录侧的状态 ----------
def _require_resource_name(name: str) -> str:
    errors = validate_name(str(name))
    if errors:
        raise ValueError(errors[0])
    return str(name)


def _skill_dst(workdir: Path, name: str) -> Path:
    return workdir / ".codex" / "skills" / _require_resource_name(name)


def _codex_skill_dst(workdir: Path, name: str) -> Path:
    return _skill_dst(workdir, name)


def _skill_dsts(workdir: Path, name: str) -> list[Path]:
    return [_skill_dst(workdir, name)]


def _disabled_skill_dst(workdir: Path, name: str) -> Path:
    return workdir / ".codex" / "skills_disabled" / _require_resource_name(name)


def _local_mcp_bundle(workdir: Path, name: str) -> Path:
    return workdir / ".codex" / "mcp" / _require_resource_name(name)


def _hub_mcp_bundle(name: str) -> Path:
    return MCP / _require_resource_name(name)


def _hub_mcp_path(name: str) -> Path:
    bundle = _hub_mcp_bundle(name) / "mcp.json"
    return bundle if bundle.is_file() else MCP / f"{name}.json"


def _mcp_path(workdir: Path) -> Path:
    return workdir / ".mcp.json"


def _codex_config_path(workdir: Path) -> Path:
    return workdir / ".codex" / "config.toml"


def _read_mcp(workdir: Path) -> dict:
    p = _mcp_path(workdir)
    if not p.exists():
        return {"mcpServers": {}}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        d = {}
    d.setdefault("mcpServers", {})
    return d


def _write_mcp(workdir: Path, data: dict) -> None:
    _mcp_path(workdir).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_codex_mcp(workdir, data.get("mcpServers", {}))


def _toml_str(value) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _toml_key(value: object) -> str:
    key = str(value)
    return key if key.replace("-", "_").replace("_", "a").isalnum() else _toml_str(key)


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{_toml_key(key)} = {_toml_value(item)}" for key, item in value.items()) + " }"
    return _toml_str(value)


def _strip_codex_mcp_tables(text: str) -> str:
    out, skip = [], False
    for line in text.splitlines():
        if line.startswith("[mcp_servers."):
            skip = True
            continue
        if skip and line.startswith("[") and not line.startswith("[mcp_servers."):
            skip = False
        if not skip:
            out.append(line)
    return "\n".join(out).rstrip()


def _append_toml_mapping(lines: list[str], table: str, mapping: dict) -> None:
    lines.append("")
    lines.append(f"[{table}]")
    for key in sorted(mapping):
        value = mapping[key]
        if value is None or isinstance(value, dict):
            continue
        lines.append(f"{_toml_key(key)} = {_toml_value(value)}")
    for key in sorted(mapping):
        value = mapping[key]
        if isinstance(value, dict) and value:
            _append_toml_mapping(lines, f"{table}.{_toml_key(key)}", value)


def _write_codex_mcp(workdir: Path, servers: dict) -> None:
    path = _codex_config_path(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    base = _strip_codex_mcp_tables(path.read_text(encoding="utf-8") if path.exists() else "")
    blocks = []
    preferred = (
        "type", "url", "command", "args", "bearer_token_env_var", "startup_timeout_sec",
        "tool_timeout_sec", "enabled",
    )
    for key, conf in sorted(servers.items()):
        if not isinstance(conf, dict):
            continue
        # .mcp.json 使用跨客户端通用的 headers；Codex TOML 的字段名是
        # http_headers。直接写成 headers 会被 Codex 忽略并导致鉴权 401。
        codex_conf = dict(conf)
        headers = codex_conf.pop("headers", None)
        if isinstance(headers, dict):
            explicit = codex_conf.get("http_headers")
            codex_conf["http_headers"] = {
                **headers,
                **(explicit if isinstance(explicit, dict) else {}),
            }
        table = f"mcp_servers.{_toml_key(key)}"
        lines = [f"[{table}]"]
        scalar_keys = [item for item in preferred if item in codex_conf and not isinstance(codex_conf[item], dict)]
        scalar_keys.extend(
            item for item in sorted(codex_conf)
            if item not in scalar_keys and not isinstance(codex_conf[item], dict) and not item.startswith("_")
        )
        for item in scalar_keys:
            value = codex_conf[item]
            if value is None:
                continue
            lines.append(f"{_toml_key(item)} = {_toml_value(value)}")
        for item in sorted(codex_conf):
            values = codex_conf[item]
            if not isinstance(values, dict) or not values or item.startswith("_"):
                continue
            _append_toml_mapping(lines, f"{table}.{_toml_key(item)}", values)
        blocks.append("\n".join(lines))
    content = "\n\n".join(x for x in (base, "\n\n".join(blocks)) if x.strip()).rstrip() + "\n"
    path.write_text(content, encoding="utf-8")


def _mcp_keys(name: str) -> list[str]:
    """hub/mcp/<名>.json 里定义的 server key 列表。"""
    p = _hub_mcp_path(name)
    if not p.exists():
        return []
    return list(load_mcp_document(p).keys())


def _materialize_mcp(value, bundle_dir: Path):
    value = config.expand_env(value)
    if isinstance(value, str):
        return value.replace(MCP_BUNDLE_TOKEN, str(bundle_dir.resolve()))
    if isinstance(value, list):
        return [_materialize_mcp(item, bundle_dir) for item in value]
    if isinstance(value, dict):
        return {key: _materialize_mcp(item, bundle_dir) for key, item in value.items()}
    return value


def _atomic_copytree(src: Path, dst: Path) -> None:
    """Copy a directory without exposing a partially-written destination."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{dst.name}-", dir=dst.parent) as tmp:
        staged = Path(tmp) / "payload"
        shutil.copytree(src, staged)
        backup = Path(tmp) / "backup"
        if dst.exists():
            dst.rename(backup)
        try:
            staged.rename(dst)
        except Exception:
            if backup.exists() and not dst.exists():
                backup.rename(dst)
            raise


def skill_loaded(workdir: Path, name: str) -> bool:
    return all(p.is_dir() for p in _skill_dsts(workdir, name))


def mcp_loaded(workdir: Path, name: str) -> bool:
    keys = _mcp_keys(name)
    if not keys:
        return False
    have = _read_mcp(workdir).get("mcpServers", {})
    return all(k in have for k in keys)


# ---------- 装 / 卸 ----------
def load_skill(workdir: Path, name: str) -> None:
    src = SKILLS / _require_resource_name(name)
    if not src.is_dir():
        return
    _atomic_copytree(src, _skill_dst(workdir, name))
    _record_resource(workdir, "skill", name)


def unload_skill(workdir: Path, name: str) -> None:
    for dst in _skill_dsts(workdir, name):
        if dst.is_dir():
            shutil.rmtree(dst)
    _forget_resource(workdir, "skill", name)


def _deep_merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def load_mcp(workdir: Path, name: str) -> None:
    src = _hub_mcp_path(name)
    if not src.exists():
        return
    servers = load_mcp_document(src)
    if not servers:
        return
    hub_bundle = _hub_mcp_bundle(name)
    if hub_bundle.is_dir():
        local_bundle = _local_mcp_bundle(workdir, name)
        _atomic_copytree(hub_bundle, local_bundle)
        servers = _materialize_mcp(servers, local_bundle)
    # 应用该群对该 mcp 的参数/版本覆盖（深合并，覆盖胜出）
    over = chatconfig.get(workdir).get("mcp_overrides", {}).get(name, {})
    if isinstance(over, dict):
        for sk, conf in servers.items():
            if sk in over and isinstance(conf, dict) and isinstance(over[sk], dict):
                _deep_merge(conf, over[sk])
    data = _read_mcp(workdir)
    state = _read_resource_state(workdir)
    previous = state.get("mcp", {}).get(name, {})
    previous_keys = set(previous.get("keys", [])) if isinstance(previous, dict) else set()
    for stale_key in previous_keys - set(servers):
        data["mcpServers"].pop(stale_key, None)
    data["mcpServers"].update(servers)
    _write_mcp(workdir, data)
    _record_resource(workdir, "mcp", name, keys=list(servers))


def unload_mcp(workdir: Path, name: str) -> None:
    data = _read_mcp(workdir)
    previous = _read_resource_state(workdir).get("mcp", {}).get(name, {})
    keys = set(_mcp_keys(name))
    if isinstance(previous, dict):
        keys.update(previous.get("keys", []))
    for k in keys:
        data["mcpServers"].pop(k, None)
    _write_mcp(workdir, data)
    bundle = _local_mcp_bundle(workdir, name)
    if bundle.is_dir():
        shutil.rmtree(bundle)
    _forget_resource(workdir, "mcp", name)


def bootstrap_resource_state(workdir: Path) -> dict:
    """为升级前已安装的资源补建状态；不会改变启用范围。"""
    state = _read_resource_state(workdir)
    changed = False
    for name in list_skills():
        if name in state["skills"] or not _skill_dst(workdir, name).is_dir():
            continue
        local_digest = _path_digest(_skill_dst(workdir, name))
        revision = resource_revision("skill", name)
        revision["digest"] = local_digest
        revision["revision"] = revision.get("version") or (f"rev-{local_digest[:12]}" if local_digest else "")
        state["skills"][name] = revision
        changed = True
    active_keys = set(_read_mcp(workdir).get("mcpServers", {}))
    for name in list_mcp():
        if name in state["mcp"]:
            continue
        keys = _mcp_keys(name)
        if not (active_keys & set(keys)):
            continue
        # 摘要留空以强制第一次对账从 Hub 刷新，兼容旧安装与 server key 改名。
        state["mcp"][name] = {"version": "", "digest": "", "revision": "", "keys": sorted(keys)}
        changed = True
    if changed:
        _write_resource_state(workdir, state)
    return state


def reconcile_loaded_resources(workdir: Path) -> list[str]:
    """刷新系统默认及本会话已加载的 Hub 资源，返回实际变化摘要。"""
    state = bootstrap_resource_state(workdir)
    changes: list[str] = []
    defaults_now = defaults()

    for name in defaults_now["skills"]:
        if name not in state["skills"]:
            load_skill(workdir, name)
            changes.append(f"skill:{name}")
    for name in defaults_now["mcp"]:
        if name not in state["mcp"]:
            load_mcp(workdir, name)
            changes.append(f"mcp:{name}")

    state = _read_resource_state(workdir)
    for name, installed in list(state["skills"].items()):
        source = SKILLS / name
        if not source.is_dir():
            unload_skill(workdir, name)
            changes.append(f"skill:{name}:removed")
            continue
        current = resource_revision("skill", name)
        if installed.get("digest") != current["digest"] or not _skill_dst(workdir, name).is_dir():
            load_skill(workdir, name)
            changes.append(f"skill:{name}")

    active = _read_mcp(workdir).get("mcpServers", {})
    state = _read_resource_state(workdir)
    for name, installed in list(state["mcp"].items()):
        source = _hub_mcp_path(name)
        if not source.exists():
            unload_mcp(workdir, name)
            changes.append(f"mcp:{name}:removed")
            continue
        current = resource_revision("mcp", name)
        current_keys = set(_mcp_keys(name))
        installed_keys = set(installed.get("keys", [])) if isinstance(installed, dict) else set()
        missing = current_keys - set(active)
        stale = (installed_keys - current_keys) & set(active)
        if installed.get("digest") != current["digest"] or missing or stale:
            load_mcp(workdir, name)
            changes.append(f"mcp:{name}")
            active = _read_mcp(workdir).get("mcpServers", {})
    return changes


def toggle_skill(workdir: Path, name: str) -> bool:
    """切换装/卸，返回切换后是否已装。"""
    if skill_loaded(workdir, name):
        unload_skill(workdir, name)
        return False
    load_skill(workdir, name)
    return True


def toggle_mcp(workdir: Path, name: str) -> bool:
    if mcp_loaded(workdir, name):
        unload_mcp(workdir, name)
        return False
    load_mcp(workdir, name)
    return True


def list_local_skills(workdir: Path) -> list[str]:
    """该群工作目录里有、但 hub 没有的 skill（= 专用 skill）。"""
    hubset = set(list_skills())
    names: set[str] = set()
    for d in (workdir / ".codex" / "skills", workdir / ".codex" / "skills_disabled"):
        if d.is_dir():
            names.update(p.name for p in d.iterdir() if p.is_dir() and p.name not in hubset)
    return sorted(names)


def list_local_mcp(workdir: Path) -> list[str]:
    """该群 .mcp.json 里有、但不属于任何 hub mcp 文件的 server key（= 专用 mcp）。"""
    have = _read_mcp(workdir).get("mcpServers", {})
    hub_keys: set[str] = set()
    for n in list_mcp():
        hub_keys.update(_mcp_keys(n))
    names = {k for k in have if k not in hub_keys}
    bundle_root = workdir / ".codex" / "mcp"
    if bundle_root.is_dir():
        names.update(p.name for p in bundle_root.iterdir() if p.is_dir() and p.name not in set(list_mcp()))
    return sorted(names)


def _local_skill_source(workdir: Path, name: str) -> Path:
    active = _skill_dst(workdir, name)
    return active if active.is_dir() else _disabled_skill_dst(workdir, name)


def _version_core(value: str) -> tuple[int, int, int]:
    try:
        return tuple(int(part) for part in value.split("-", 1)[0].split(".")[:3])  # type: ignore[return-value]
    except (TypeError, ValueError):
        return (-1, -1, -1)


def validate_skill(workdir: Path, name: str, *, publish: bool = False) -> ValidationReport:
    return validate_skill_dir(_local_skill_source(workdir, name), name, publish=publish)


def publish_skill(workdir: Path, name: str, overwrite: bool = False) -> ValidationReport:
    """Validate, generalize-gate, then atomically publish a dedicated Skill."""
    report = validate_skill(workdir, name, publish=True)
    dst = SKILLS / _require_resource_name(name)
    if dst.exists() and not overwrite:
        report.errors.append("Hub 已存在同名 Skill；更新时必须显式使用 --update 并提升 VERSION")
    if dst.exists() and overwrite and report.ok:
        old = skill_meta(name).get("version", "")
        new = report.details.get("version", "")
        if old and new and _version_core(new) <= _version_core(old):
            report.errors.append(f"更新通用 Skill 前必须提升 VERSION（Hub {old}，待发布 {new}）")
    if not report.ok:
        return report
    _atomic_copytree(_local_skill_source(workdir, name), dst)
    report.details["published"] = True
    report.details["scope"] = "general"
    return report


def promote_skill(workdir: Path, name: str, overwrite: bool = False) -> bool:
    """Backward-compatible boolean wrapper for checked Skill publication."""
    return publish_skill(workdir, name, overwrite=overwrite).ok


def _local_mcp_document(workdir: Path, name: str) -> tuple[dict, Path | None]:
    bundle = _local_mcp_bundle(workdir, name)
    definition = bundle / "mcp.json"
    if definition.is_file():
        document = load_mcp_document(definition)
        payload_files = {
            item.relative_to(bundle).as_posix()
            for item in bundle.rglob("*")
            if item.is_file()
        } - {"mcp.json", "VERSION"}
        serialized = json.dumps(document, ensure_ascii=False)
        if payload_files or MCP_BUNDLE_TOKEN in serialized:
            return document, bundle
    active = _read_mcp(workdir).get("mcpServers", {})
    disabled = chatconfig.get(workdir).get("_disabled_mcp", {})
    conf = active.get(name) or disabled.get(name)
    if isinstance(conf, dict):
        return {name: conf}, None
    if definition.is_file():
        return load_mcp_document(definition), None
    return {}, None


def validate_mcp(workdir: Path, name: str, *, publish: bool = False) -> ValidationReport:
    document, bundle = _local_mcp_document(workdir, name)
    path = bundle / "mcp.json" if bundle else _mcp_path(workdir)
    return validate_mcp_definition(name, document, path=path, bundle_dir=bundle, publish=publish)


def register_local_mcp(workdir: Path, name: str) -> ValidationReport:
    """Activate a dedicated MCP config and leave reload authority to the control panel user."""
    document, bundle = _local_mcp_document(workdir, name)
    report = validate_mcp_definition(
        name,
        document,
        path=(bundle / "mcp.json" if bundle else _mcp_path(workdir)),
        bundle_dir=bundle,
        publish=False,
    )
    if not report.ok:
        return report
    data = _read_mcp(workdir)
    servers = _materialize_mcp(document, bundle) if bundle else document
    data["mcpServers"].update(servers)
    _write_mcp(workdir, data)
    mark_resource_updates(workdir, [f"mcp:{name}"])
    report.details["registered"] = True
    report.details["storage"] = "bundle" if bundle else "config"
    return report


def publish_mcp(workdir: Path, name: str, overwrite: bool = False) -> ValidationReport:
    """Validate and publish a config-only MCP or a portable generated MCP bundle."""
    document, bundle = _local_mcp_document(workdir, name)
    report = validate_mcp_definition(
        name,
        document,
        path=(bundle / "mcp.json" if bundle else _mcp_path(workdir)),
        bundle_dir=bundle,
        publish=True,
    )
    existing_file = MCP / f"{_require_resource_name(name)}.json"
    existing_bundle = _hub_mcp_bundle(name)
    exists = existing_file.exists() or existing_bundle.exists()
    if exists and not overwrite:
        report.errors.append("Hub 已存在同名 MCP；更新时必须显式使用 --update")
    if exists and overwrite and bundle and report.ok:
        old = mcp_meta(name).get("version", "")
        new = report.details.get("version", "")
        if old and new and _version_core(new) <= _version_core(old):
            report.errors.append(f"更新通用 MCP bundle 前必须提升 VERSION（Hub {old}，待发布 {new}）")
    if not report.ok:
        return report

    if bundle:
        _atomic_copytree(bundle, existing_bundle)
        if existing_file.is_file():
            existing_file.unlink()
        report.details["bundle"] = True
    else:
        MCP.mkdir(parents=True, exist_ok=True)
        staged = existing_file.with_suffix(".json.tmp")
        staged.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        staged.replace(existing_file)
        if existing_bundle.is_dir():
            shutil.rmtree(existing_bundle)
        local_legacy_bundle = _local_mcp_bundle(workdir, name)
        if local_legacy_bundle.is_dir():
            payload = {
                item.relative_to(local_legacy_bundle).as_posix()
                for item in local_legacy_bundle.rglob("*")
                if item.is_file()
            } - {"mcp.json", "VERSION"}
            if not payload:
                shutil.rmtree(local_legacy_bundle)
        report.details["bundle"] = False
    report.details["published"] = True
    report.details["scope"] = "general"
    return report


def promote_mcp(workdir: Path, server_key: str, overwrite: bool = False) -> bool:
    """Backward-compatible boolean wrapper for checked MCP publication."""
    return publish_mcp(workdir, server_key, overwrite=overwrite).ok


# ---------- git（hub 若是 git 仓库可提交版本）----------
def is_git() -> bool:
    return (HUB / ".git").is_dir()


def git_commit(message: str, paths: list[str] | None = None) -> str:
    import subprocess

    if not is_git():
        return "hub 非 git 仓库（跳过提交）。可用 git init 初始化。"
    selected = paths
    if paths:
        selected = []
        for path in paths:
            tracked = subprocess.run(
                ["git", "-C", str(HUB), "ls-files", "--", path], capture_output=True, text=True
            ).stdout.strip()
            if (HUB / path).exists() or tracked:
                selected.append(path)
                subprocess.run(
                    ["git", "-C", str(HUB), "add", "-A", "--", path], capture_output=True, text=True
                )
        if not selected:
            return "没有需要提交的资源文件。"
    else:
        subprocess.run(["git", "-C", str(HUB), "add", "-A"], capture_output=True, text=True)
    r = subprocess.run(
        ["git", "-C", str(HUB), "-c", "user.email=bot@chat-agent", "-c", "user.name=chat-agent",
         "commit", "-m", message, *(["--", *selected] if selected else [])],
        capture_output=True, text=True,
    )
    return (r.stdout + r.stderr).strip() or "已提交"


def skill_meta(name: str) -> dict:
    """从 SKILL.md frontmatter 和 VERSION 读说明 / 版本 / 文件数。"""
    import re

    d = SKILLS / _require_resource_name(name)
    desc = ""
    version = ""
    md = d / "SKILL.md"
    if md.exists():
        txt = md.read_text(encoding="utf-8")
        m = re.search(r"^---\s*(.*?)\s*---", txt, re.S | re.M)
        if m:
            for line in m.group(1).splitlines():
                if line.lower().startswith("description:"):
                    desc = line.split(":", 1)[1].strip()
    vf = d / "VERSION"
    if vf.exists():
        version = vf.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    files = sum(1 for p in d.rglob("*") if p.is_file()) if d.is_dir() else 0
    return {"name": name, "description": desc, "version": version, "files": files}


def mcp_meta(name: str) -> dict:
    """读 mcp json：包含的 server keys + 第一个 server 的命令。"""
    p = _hub_mcp_path(name)
    servers, cmd = [], ""
    if p.exists():
        data = load_mcp_document(p)
        servers = list(data.keys())
        if servers:
            cmd = str(data[servers[0]].get("command", ""))
    version = ""
    version_path = _hub_mcp_bundle(name) / "VERSION"
    if version_path.is_file():
        version = version_path.read_text(encoding="utf-8").strip().splitlines()[0]
    revision = resource_revision("mcp", name)
    return {
        "name": name,
        "servers": servers,
        "command": cmd,
        "version": version or revision["revision"],
        "digest": revision["digest"],
        "bundle": p.parent.name == name,
    }


# ---------- 三分类：系统(默认) / 通用(hub非默认) / 专用(本群独有) ----------
def system_skills() -> list[str]:
    dset = set(defaults()["skills"])
    return [n for n in list_skills() if n in dset]


def general_skills() -> list[str]:
    dset = set(defaults()["skills"])
    return [n for n in list_skills() if n not in dset]


def system_mcp() -> list[str]:
    dset = set(defaults()["mcp"])
    return [n for n in list_mcp() if n in dset]


def general_mcp() -> list[str]:
    dset = set(defaults()["mcp"])
    return [n for n in list_mcp() if n not in dset]


def _wd_skill_names(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.is_dir() else []


def special_skills(workdir: Path) -> list[str]:
    """本群独有 skill（不在 hub）：活跃的 + 已暂存禁用的。"""
    hubset = set(list_skills())
    names: set[str] = set()
    for d in (workdir / ".codex" / "skills", workdir / ".codex" / "skills_disabled"):
        names.update(n for n in _wd_skill_names(d) if n not in hubset)
    return sorted(names)


def special_skill_enabled(workdir: Path, name: str) -> bool:
    return _skill_dst(workdir, name).is_dir()


def enable_special_skill(workdir: Path, name: str) -> None:
    src = _disabled_skill_dst(workdir, name)
    dst = _skill_dst(workdir, name)
    if not src.is_dir():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)


def disable_special_skill(workdir: Path, name: str) -> None:
    src = _skill_dst(workdir, name)
    if not src.is_dir():
        return
    dst = _disabled_skill_dst(workdir, name)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.rmtree(dst)
    src.rename(dst)


def delete_special_skill(workdir: Path, name: str) -> None:
    """彻底删除某群专用 skill（活跃 + 暂存都删）。"""
    for d in (_skill_dst(workdir, name), _disabled_skill_dst(workdir, name)):
        if d.is_dir():
            shutil.rmtree(d)


def delete_special_mcp(workdir: Path, key: str) -> None:
    """彻底删除某群专用 mcp（活跃 + 暂存都删）。"""
    data = _read_mcp(workdir)
    if key in data.get("mcpServers", {}):
        data["mcpServers"].pop(key, None)
        _write_mcp(workdir, data)
    dm = dict(chatconfig.get(workdir).get("_disabled_mcp", {}))
    if key in dm:
        dm.pop(key, None)
        chatconfig.set_value(workdir, "_disabled_mcp", dm)
    bundle = _local_mcp_bundle(workdir, key)
    if bundle.is_dir():
        shutil.rmtree(bundle)


def _desc_of(md: Path) -> str:
    import re

    if not md.exists():
        return ""
    m = re.search(r"^---\s*(.*?)\s*---", md.read_text(encoding="utf-8"), re.S | re.M)
    if m:
        for line in m.group(1).splitlines():
            if line.lower().startswith("description:"):
                return line.split(":", 1)[1].strip()
    return ""


def skill_desc(workdir: Path, name: str) -> str:
    """任意来源（hub / 本群活跃 / 本群暂存）找该 skill 的说明。"""
    safe_name = _require_resource_name(name)
    for base in (SKILLS / safe_name, _skill_dst(workdir, safe_name), _disabled_skill_dst(workdir, safe_name)):
        d = _desc_of(base / "SKILL.md")
        if d:
            vf = base / "VERSION"
            version = vf.read_text(encoding="utf-8").strip().splitlines()[0].strip() if vf.exists() else ""
            return f"v{version}\n{d}" if version else d
    return ""


# ---------- 专用 MCP 启停（暂存到 agent.json 的 _disabled_mcp）----------
def special_mcp(workdir: Path) -> list[str]:
    hub_keys: set[str] = set()
    for n in list_mcp():
        hub_keys.update(_mcp_keys(n))
    active = [k for k in _read_mcp(workdir).get("mcpServers", {}) if k not in hub_keys]
    disabled = list(chatconfig.get(workdir).get("_disabled_mcp", {}).keys())
    bundle_root = workdir / ".codex" / "mcp"
    bundles = [p.name for p in bundle_root.iterdir() if p.is_dir()] if bundle_root.is_dir() else []
    return sorted((set(active) | set(disabled) | set(bundles)) - set(list_mcp()))


def special_mcp_enabled(workdir: Path, key: str) -> bool:
    return key in _read_mcp(workdir).get("mcpServers", {})


def enable_special_mcp(workdir: Path, key: str) -> None:
    dm = dict(chatconfig.get(workdir).get("_disabled_mcp", {}))
    if key in dm:
        data = _read_mcp(workdir)
        data["mcpServers"][key] = dm.pop(key)
        _write_mcp(workdir, data)
        chatconfig.set_value(workdir, "_disabled_mcp", dm)
    elif (_local_mcp_bundle(workdir, key) / "mcp.json").is_file():
        register_local_mcp(workdir, key)


def disable_special_mcp(workdir: Path, key: str) -> None:
    data = _read_mcp(workdir)
    if key in data.get("mcpServers", {}):
        conf = data["mcpServers"].pop(key)
        _write_mcp(workdir, data)
        dm = dict(chatconfig.get(workdir).get("_disabled_mcp", {}))
        dm[key] = conf
        chatconfig.set_value(workdir, "_disabled_mcp", dm)


def mcp_desc(workdir: Path, key: str) -> str:
    if key in list_mcp():
        meta = mcp_meta(key)
        kind = "可移植 bundle" if meta["bundle"] else "配置型 MCP"
        servers = "、".join(meta["servers"]) or "（无）"
        command = f"\n命令: {meta['command']}" if meta["command"] else ""
        return f"版本: {meta['version'] or '未标记'}\n类型: {kind}\nServers: {servers}{command}"
    have = _read_mcp(workdir).get("mcpServers", {})
    conf = have.get(key) or chatconfig.get(workdir).get("_disabled_mcp", {}).get(key, {})
    bundle = _local_mcp_bundle(workdir, key)
    if not conf and (bundle / "mcp.json").is_file():
        conf = load_mcp_document(bundle / "mcp.json").get(key, {})
    cmd = conf.get("command", "") if isinstance(conf, dict) else ""
    suffix = "\n类型: 可移植 bundle" if bundle.is_dir() else ""
    return (f"命令: {cmd}" if cmd else "（自定义 MCP）") + suffix


def promote_special_mcp(workdir: Path, key: str, overwrite: bool = False) -> bool:
    """发布专用 mcp（活跃或暂存的都支持）到 hub。"""
    return publish_mcp(workdir, key, overwrite=overwrite).ok


def apply_defaults(workdir: Path) -> None:
    """把系统默认项同步到工作目录，并卸掉此前由系统默认装入但已移除的项。"""
    d = defaults()
    prev = _read_default_state(workdir)

    for name in set(prev["skills"]) - set(d["skills"]):
        unload_skill(workdir, name)
    for name in set(prev["mcp"]) - set(d["mcp"]):
        unload_mcp(workdir, name)

    # 系统资源以 Hub 为真相源；每次启动都刷新，确保对话生命周期规则和 MCP 配置升级能到达现有群。
    for name in d["skills"]:
        load_skill(workdir, name)
    for name in d["mcp"]:
        load_mcp(workdir, name)
    _write_default_state(workdir, d)
