#!/usr/bin/env python3
"""CLI used by Codex to manage scheduled tasks for the current conversation."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path


ROOT = Path(os.environ.get("CHAT_AGENT_HOME") or Path(__file__).resolve().parent.parent).resolve()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils import scheduled  # noqa: E402


WEEKDAYS = {
    "mon": 0, "monday": 0,
    "tue": 1, "tuesday": 1,
    "wed": 2, "wednesday": 2,
    "thu": 3, "thursday": 3,
    "fri": 4, "friday": 4,
    "sat": 5, "saturday": 5,
    "sun": 6, "sunday": 6,
}


def _workdir(args: argparse.Namespace) -> Path:
    return Path(args.workdir or os.getcwd()).expanduser().resolve()


def _chat_id(args: argparse.Namespace) -> str:
    value = str(args.chat_id or os.environ.get("CHAT_ID") or "").strip()
    if not value:
        raise ValueError("CHAT_ID 缺失；请在 chat-agent 对话中运行，或传 --chat-id")
    return value


def _duration(value: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    parts = re.findall(r"(\d+)\s*([smhdw])", value.lower())
    if not parts or "".join(f"{n}{u}" for n, u in parts) != re.sub(r"\s+", "", value.lower()):
        raise ValueError("间隔格式错误，例如 30m、2h、1d 或 1h30m")
    return sum(int(number) * units[unit] for number, unit in parts)


def _weekdays(value: str | None) -> list[int] | None:
    if not value or value.lower() in {"all", "daily", "everyday"}:
        return None
    value = value.lower().strip()
    if value in {"weekdays", "mon-fri"}:
        return [0, 1, 2, 3, 4]
    if value in {"weekends", "sat-sun"}:
        return [5, 6]
    days = []
    for token in value.split(","):
        token = token.strip()
        if token not in WEEKDAYS:
            raise ValueError(f"无效星期：{token}")
        days.append(WEEKDAYS[token])
    return days


def _schedule(args: argparse.Namespace) -> dict | None:
    if getattr(args, "once", None):
        return {"kind": "once", "run_at": args.once}
    if getattr(args, "interval", None):
        value = {"kind": "interval", "every_seconds": _duration(args.interval)}
        if getattr(args, "start_at", None):
            value["anchor_at"] = args.start_at
        return value
    if getattr(args, "daily", None):
        return {"kind": "daily", "time": args.daily, "weekdays": _weekdays(args.weekdays)}
    if getattr(args, "cron", None):
        return {"kind": "cron", "expression": args.cron}
    return None


def _prompt(args: argparse.Namespace, *, optional: bool = False) -> str | None:
    if getattr(args, "prompt_file", None):
        path = Path(args.prompt_file).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"找不到 prompt 文件：{path}")
        return path.read_text(encoding="utf-8")
    if getattr(args, "prompt", None) is not None:
        return args.prompt
    if optional:
        return None
    raise ValueError("必须传 --prompt 或 --prompt-file")


def _view(task: dict) -> dict:
    result = dict(task)
    result["schedule_text"] = scheduled.schedule_text(task)
    return result


def _resolve(workdir: Path, reference: str) -> dict:
    return scheduled.resolve_task(workdir, reference)


def do_create(args: argparse.Namespace) -> dict:
    workdir = _workdir(args)
    task = scheduled.create_task(
        workdir,
        _chat_id(args),
        args.name,
        _prompt(args) or "",
        _schedule(args) or {},
        timezone_name=args.timezone,
        enabled=not args.paused,
    )
    if args.run_now:
        task = scheduled.request_run(workdir, task["id"])
    return {"ok": True, "action": "created", "task": _view(task)}


def do_list(args: argparse.Namespace) -> dict:
    tasks = [_view(task) for task in scheduled.list_tasks(_workdir(args))]
    return {"ok": True, "count": len(tasks), "tasks": tasks}


def do_migrate(args: argparse.Namespace) -> dict:
    result = scheduled.migrate_legacy_tasks(_workdir(args))
    return {
        "ok": True,
        "action": "migrated",
        "database": str(scheduled.database_path()),
        **result,
    }


def do_show(args: argparse.Namespace) -> dict:
    return {"ok": True, "task": _view(_resolve(_workdir(args), args.task))}


def do_pause(args: argparse.Namespace) -> dict:
    workdir = _workdir(args)
    task = _resolve(workdir, args.task)
    return {"ok": True, "action": "paused", "task": _view(scheduled.pause_task(workdir, task["id"]))}


def do_resume(args: argparse.Namespace) -> dict:
    workdir = _workdir(args)
    task = _resolve(workdir, args.task)
    return {"ok": True, "action": "resumed", "task": _view(scheduled.resume_task(workdir, task["id"]))}


def do_run(args: argparse.Namespace) -> dict:
    workdir = _workdir(args)
    task = _resolve(workdir, args.task)
    return {"ok": True, "action": "run_requested", "task": _view(scheduled.request_run(workdir, task["id"]))}


def do_delete(args: argparse.Namespace) -> dict:
    workdir = _workdir(args)
    task = _resolve(workdir, args.task)
    scheduled.delete_task(workdir, task["id"])
    return {"ok": True, "action": "deleted", "id": task["id"], "name": task["name"]}


def do_update(args: argparse.Namespace) -> dict:
    workdir = _workdir(args)
    task = _resolve(workdir, args.task)
    updated = scheduled.update_task(
        workdir,
        task["id"],
        name=args.name,
        prompt=_prompt(args, optional=True),
        schedule=_schedule(args),
        timezone_name=args.timezone,
    )
    return {"ok": True, "action": "updated", "task": _view(updated)}


def _schedule_args(parser: argparse.ArgumentParser, *, required: bool) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--once", help="一次性本地/ISO 时间，如 2026-07-22T09:00")
    group.add_argument("--interval", help="固定间隔，如 30m、2h、1d")
    group.add_argument("--daily", help="每日时间 HH:MM，可配 --weekdays")
    group.add_argument("--cron", help="五段 Cron：分 时 日 月 周")
    parser.add_argument("--weekdays", help="all、weekdays、weekends 或 mon,wed,fri")
    parser.add_argument("--start-at", help="interval 的 ISO 锚点时间")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage chat-agent scheduled tasks.")
    parser.add_argument("--workdir", default="", help="对话工作目录，默认当前目录")
    parser.add_argument("--chat-id", default="", help="默认使用 CHAT_ID")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create", help="创建任务")
    create.add_argument("--name", required=True)
    prompt = create.add_mutually_exclusive_group(required=True)
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file")
    create.add_argument("--timezone", default=scheduled.DEFAULT_TIMEZONE)
    create.add_argument("--paused", action="store_true")
    create.add_argument("--run-now", action="store_true")
    _schedule_args(create, required=True)
    create.set_defaults(fn=do_create)

    listing = sub.add_parser("list", help="列出任务")
    listing.set_defaults(fn=do_list)
    migrate = sub.add_parser("migrate", help="把当前工作目录的旧 JSON 任务导入 SQLite")
    migrate.set_defaults(fn=do_migrate)
    show = sub.add_parser("show", help="查看详情")
    show.add_argument("task")
    show.set_defaults(fn=do_show)
    for command, fn in (("pause", do_pause), ("resume", do_resume), ("run-now", do_run), ("delete", do_delete)):
        item = sub.add_parser(command)
        item.add_argument("task")
        item.set_defaults(fn=fn)

    update = sub.add_parser("update", help="修改任务")
    update.add_argument("task")
    update.add_argument("--name")
    prompt = update.add_mutually_exclusive_group()
    prompt.add_argument("--prompt")
    prompt.add_argument("--prompt-file")
    update.add_argument("--timezone")
    _schedule_args(update, required=False)
    update.set_defaults(fn=do_update)
    return parser


def main() -> None:
    try:
        args = build_parser().parse_args()
        print(json.dumps(args.fn(args), ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
