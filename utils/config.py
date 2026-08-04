"""Local configuration loading and process-wide secret redaction.

Public defaults live in ``config.json`` and may reference ``${ENV_VAR}`` values.
Secrets belong in the ignored ``.env`` file or the process environment.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
CONFIG_PATH = ROOT / "config.json"
_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_SENSITIVE_NAME_RE = re.compile(
    r"(?i)(?:secret|password|passwd|token|api[_-]?key|authorization|cookie|ticket|access[_-]?key)"
)
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:access_key|ticket|token|access_token|refresh_token|api_key)=)[^&#\s]+"
)
_BEARER_RE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+")
_ASSIGNMENT_RE = re.compile(
    r"(?i)((?:secret|password|passwd|token|api[_-]?key|authorization|cookie|ticket|access[_-]?key)"
    r"[\"']?\s*[:=]\s*[\"']?)[^\s,\"'}]+"
)
_loaded_env = False
_redaction_installed = False


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_dotenv(path: Path = ENV_PATH, *, override: bool = False) -> None:
    """Load a small, predictable subset of dotenv syntax without a dependency."""
    global _loaded_env
    if _loaded_env and path == ENV_PATH:
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        if override or key not in os.environ:
            os.environ[key] = _unquote(value)
    if path == ENV_PATH:
        _loaded_env = True


def _expand(value: Any, *, keep_missing: bool = False) -> Any:
    if isinstance(value, dict):
        return {key: _expand(item, keep_missing=keep_missing) for key, item in value.items()}
    if isinstance(value, list):
        return [_expand(item, keep_missing=keep_missing) for item in value]
    if not isinstance(value, str):
        return value
    def replace(match: re.Match) -> str:
        name = match.group(1)
        if name in os.environ:
            return os.environ[name]
        return match.group(0) if keep_missing else ""
    return _ENV_REF_RE.sub(replace, value)


def load(path: Path = CONFIG_PATH) -> dict:
    load_dotenv()
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"配置文件不存在：{path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"配置文件不是有效 JSON：{path}: {exc}") from exc
    if not isinstance(document, dict):
        raise RuntimeError(f"配置文件必须是 JSON 对象：{path}")
    return _expand(document)


def expand_env(value: Any) -> Any:
    """Expand environment references in Hub/MCP documents."""
    load_dotenv()
    return _expand(value, keep_missing=True)


def require(config: dict, *path: str) -> str:
    current: Any = config
    for key in path:
        current = current.get(key) if isinstance(current, dict) else None
    value = str(current or "").strip()
    if not value:
        dotted = ".".join(path)
        raise RuntimeError(f"缺少必需配置 {dotted}；请复制 .env.example 为 .env 并填写")
    return value


def _known_secret_values() -> list[str]:
    values = []
    for key, value in os.environ.items():
        project_secret = key.startswith(("FEISHU_", "MCP_"))
        if (_SENSITIVE_NAME_RE.search(key) or project_secret) and len(value) >= 6:
            values.append(value)
    return sorted(set(values), key=len, reverse=True)


def redact_text(value: object) -> str:
    text = str(value)
    text = _QUERY_SECRET_RE.sub(r"\1[REDACTED]", text)
    text = _BEARER_RE.sub(r"\1[REDACTED]", text)
    text = _ASSIGNMENT_RE.sub(r"\1[REDACTED]", text)
    for secret in _known_secret_values():
        text = text.replace(secret, "[REDACTED]")
    return text


def install_log_redaction() -> None:
    """Redact secrets from standard logging, including third-party SDK logs."""
    global _redaction_installed
    if _redaction_installed:
        return
    load_dotenv()
    previous = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = previous(*args, **kwargs)
        try:
            rendered = record.getMessage()
            record.msg = redact_text(rendered)
            record.args = ()
        except Exception:
            pass
        return record

    logging.setLogRecordFactory(factory)
    _redaction_installed = True
