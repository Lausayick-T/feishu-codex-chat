"""Per-conversation local data inventory, retention, and scoped cleanup."""

from __future__ import annotations

import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


RETENTION_OPTIONS = (7, 30, 90, 0)
_LAST_CLEANUP: dict[str, float] = {}


def _scoped(workdir: Path, relative: str) -> Path:
    root = workdir.expanduser().resolve()
    target = (root / relative).resolve()
    if target == root or root not in target.parents:
        raise ValueError("拒绝清理会话目录之外的路径")
    return target


def _stats(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    count = 0
    size = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() or item.is_symlink():
                count += 1
                size += item.lstat().st_size
        except OSError:
            continue
    return count, size


def format_bytes(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def inventory(workdir: Path) -> dict[str, dict[str, int]]:
    paths = {
        "memory": _scoped(workdir, "memory"),
        "attachments": _scoped(workdir, "incoming"),
        "workspace": _scoped(workdir, "workspace"),
        "task_logs": _scoped(workdir, "scheduled_tasks/runs"),
    }
    result = {}
    for name, path in paths.items():
        count, size = _stats(path)
        result[name] = {"files": count, "bytes": size}
    try:
        from . import scheduled
        result["task_logs"]["records"] = scheduled.run_history_count(workdir)
    except Exception:
        result["task_logs"]["records"] = 0
    return result


def _remove_contents(path: Path) -> int:
    if not path.exists():
        return 0
    removed = 0
    for item in list(path.iterdir()):
        if item.is_symlink() or item.is_file():
            item.unlink(missing_ok=True)
            removed += 1
        elif item.is_dir():
            removed += sum(1 for child in item.rglob("*") if child.is_file() or child.is_symlink())
            shutil.rmtree(item)
    return removed


def clear_category(workdir: Path, category: str) -> int:
    if category == "attachments":
        path = _scoped(workdir, "incoming")
        removed = _remove_contents(path)
        path.mkdir(parents=True, exist_ok=True)
        return removed
    if category == "workspace":
        path = _scoped(workdir, "workspace")
        removed = _remove_contents(path)
        path.mkdir(parents=True, exist_ok=True)
        return removed
    if category == "memory":
        path = _scoped(workdir, "memory")
        removed = _remove_contents(path)
        from . import chatconfig
        chatconfig.apply_memory_scaffold(workdir, chatconfig.get(workdir).get("memory_mode", "resume"))
        return removed
    if category == "task_logs":
        path = _scoped(workdir, "scheduled_tasks/runs")
        removed = _remove_contents(path)
        path.mkdir(parents=True, exist_ok=True)
        from . import scheduled
        removed += scheduled.clear_run_history(workdir)
        return removed
    raise ValueError("未知的数据分类")


def _prune_files(path: Path, cutoff: float) -> int:
    if not path.exists():
        return 0
    removed = 0
    for item in sorted(path.rglob("*"), key=lambda value: len(value.parts), reverse=True):
        try:
            if item.is_symlink() or item.is_file():
                if item.lstat().st_mtime < cutoff:
                    item.unlink(missing_ok=True)
                    removed += 1
            elif item.is_dir() and not any(item.iterdir()):
                item.rmdir()
        except OSError:
            continue
    return removed


def cleanup_expired(workdir: Path, *, now: float | None = None) -> dict[str, int]:
    from . import chatconfig, scheduled
    cfg = chatconfig.get(workdir)
    timestamp = time.time() if now is None else now
    removed = {"attachments": 0, "task_logs": 0}
    attachment_days = int(cfg.get("attachment_retention_days", 30) or 0)
    if attachment_days > 0:
        removed["attachments"] = _prune_files(
            _scoped(workdir, "incoming"), timestamp - attachment_days * 86400
        )
    task_days = int(cfg.get("task_log_retention_days", 30) or 0)
    if task_days > 0:
        cutoff = timestamp - task_days * 86400
        removed["task_logs"] = _prune_files(_scoped(workdir, "scheduled_tasks/runs"), cutoff)
        before = datetime.fromtimestamp(cutoff, tz=timezone.utc)
        removed["task_logs"] += scheduled.prune_run_history(workdir, before=before)
    return removed


def cleanup_if_due(workdir: Path, *, interval_seconds: int = 3600) -> dict[str, int]:
    key = str(workdir.expanduser().resolve())
    now = time.time()
    if now - _LAST_CLEANUP.get(key, 0) < interval_seconds:
        return {"attachments": 0, "task_logs": 0}
    _LAST_CLEANUP[key] = now
    return cleanup_expired(workdir, now=now)
