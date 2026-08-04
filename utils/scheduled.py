"""SQLite-backed scheduled task storage and schedule calculation."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_TIMEZONE = "Asia/Shanghai"
TASKS_DIR = "scheduled_tasks"
UTC = timezone.utc
CHAT_AGENT_ROOT = Path(__file__).resolve().parent.parent
SCHEDULER_DB = Path(
    os.environ.get("CHAT_AGENT_SCHEDULER_DB") or CHAT_AGENT_ROOT / "state" / "scheduler.db"
).resolve()
SCHEDULER_HEARTBEAT = CHAT_AGENT_ROOT / "state" / "scheduler.heartbeat.json"


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"无效时区：{name}") from exc


def _root(workdir: Path) -> Path:
    return workdir.resolve() / TASKS_DIR


def _legacy_tasks_dir(workdir: Path) -> Path:
    return _root(workdir) / "tasks"


def runs_dir(workdir: Path, task_id: str) -> Path:
    return _root(workdir) / "runs" / task_id


def database_path() -> Path:
    return SCHEDULER_DB


@contextmanager
def _connection():
    SCHEDULER_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(SCHEDULER_DB, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                workdir TEXT NOT NULL,
                id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                next_run_at TEXT,
                last_status TEXT NOT NULL,
                manual_run_requested_at TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (workdir, id)
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_workdir
                ON tasks (workdir);
            CREATE INDEX IF NOT EXISTS idx_tasks_due
                ON tasks (status, next_run_at, last_status);
            CREATE TABLE IF NOT EXISTS task_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                workdir TEXT NOT NULL,
                task_id TEXT NOT NULL,
                finished_at TEXT NOT NULL,
                status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '',
                output TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (workdir, task_id)
                    REFERENCES tasks (workdir, id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_task_runs_task
                ON task_runs (workdir, task_id, run_id DESC);
            CREATE TABLE IF NOT EXISTS legacy_imports (
                workdir TEXT NOT NULL,
                task_id TEXT NOT NULL,
                source_path TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                source_updated_at TEXT,
                PRIMARY KEY (workdir, task_id)
            );
            CREATE TABLE IF NOT EXISTS scheduler_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                status TEXT NOT NULL,
                pid INTEGER,
                updated_at TEXT NOT NULL
            );
            PRAGMA user_version = 2;
            """
        )
        legacy_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(legacy_imports)").fetchall()
        }
        if "source_updated_at" not in legacy_columns:
            conn.execute("ALTER TABLE legacy_imports ADD COLUMN source_updated_at TEXT")
        yield conn
    finally:
        conn.close()


@contextmanager
def _transaction():
    with _connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()


def _read(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _workdir_key(workdir: Path) -> str:
    return str(workdir.expanduser().resolve())


def _timestamp_is_newer(candidate: str | None, current: str | None) -> bool:
    if not candidate:
        return False
    if not current:
        return True
    try:
        return parse_iso(candidate) > parse_iso(current)
    except (TypeError, ValueError):
        return str(candidate) > str(current)


def _task_from_row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    try:
        task = json.loads(row["payload_json"])
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError(f"SQLite 中的任务数据损坏：{row['id']}") from exc
    if not isinstance(task, dict):
        raise RuntimeError(f"SQLite 中的任务数据不是对象：{row['id']}")
    return task


def _task_row(conn: sqlite3.Connection, workdir: Path, task_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT id, payload_json FROM tasks WHERE workdir = ? AND id = ?",
        (_workdir_key(workdir), task_id),
    ).fetchone()


def _save_task(conn: sqlite3.Connection, workdir: Path, task: dict) -> None:
    key = _workdir_key(workdir)
    payload = json.dumps(task, ensure_ascii=False, separators=(",", ":"))
    values = (
        payload,
        str(task.get("status") or "paused"),
        task.get("next_run_at"),
        str(task.get("last_status") or "never"),
        task.get("manual_run_requested_at"),
        str(task.get("updated_at") or iso_utc(utc_now())),
        key,
        str(task["id"]),
    )
    cursor = conn.execute(
        """
        UPDATE tasks
        SET payload_json = ?, status = ?, next_run_at = ?, last_status = ?,
            manual_run_requested_at = ?, updated_at = ?
        WHERE workdir = ? AND id = ?
        """,
        values,
    )
    if cursor.rowcount:
        return
    conn.execute(
        """
        INSERT INTO tasks (
            payload_json, status, next_run_at, last_status,
            manual_run_requested_at, updated_at, workdir, id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        values,
    )


def migrate_legacy_tasks(workdir: Path) -> dict:
    """Import legacy per-task JSON once, leaving source files untouched."""
    workdir = workdir.expanduser().resolve()
    directory = _legacy_tasks_dir(workdir)
    paths = sorted(directory.glob("*.json")) if directory.is_dir() else []
    result = {"imported": 0, "refreshed": 0, "already_migrated": 0, "invalid": 0}
    if not paths:
        return result

    now_text = iso_utc(utc_now())
    with _transaction() as conn:
        for path in paths:
            task = _read(path)
            if not task or not str(task.get("id") or "").strip():
                result["invalid"] += 1
                continue
            task_id = str(task["id"])
            migrated = conn.execute(
                """
                SELECT source_updated_at
                FROM legacy_imports
                WHERE workdir = ? AND task_id = ?
                """,
                (_workdir_key(workdir), task_id),
            ).fetchone()
            if migrated:
                stored = _task_from_row(_task_row(conn, workdir, task_id))
                source_updated = str(task.get("updated_at") or "")
                stored_updated = str((stored or {}).get("updated_at") or "")
                previous_source_updated = str(migrated["source_updated_at"] or "")
                database_unchanged_since_import = (
                    not previous_source_updated
                    or not _timestamp_is_newer(stored_updated, previous_source_updated)
                )
                marker_source_updated = previous_source_updated or source_updated
                if (
                    stored is not None
                    and database_unchanged_since_import
                    and _timestamp_is_newer(source_updated, stored_updated)
                ):
                    _save_task(conn, workdir, task)
                    result["refreshed"] += 1
                    marker_source_updated = source_updated
                else:
                    result["already_migrated"] += 1
                    if stored is None or not _timestamp_is_newer(source_updated, stored_updated):
                        marker_source_updated = source_updated or previous_source_updated
                conn.execute(
                    """
                    UPDATE legacy_imports
                    SET source_path = ?, source_updated_at = ?
                    WHERE workdir = ? AND task_id = ?
                    """,
                    (
                        str(path.resolve()),
                        marker_source_updated or None,
                        _workdir_key(workdir),
                        task_id,
                    ),
                )
                continue
            if _task_row(conn, workdir, task_id) is None:
                _save_task(conn, workdir, task)
                result["imported"] += 1
            else:
                result["already_migrated"] += 1
            conn.execute(
                """
                INSERT INTO legacy_imports (
                    workdir, task_id, source_path, imported_at, source_updated_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    _workdir_key(workdir),
                    task_id,
                    str(path.resolve()),
                    now_text,
                    task.get("updated_at"),
                ),
            )
    return result


def _parse_clock(value: str) -> datetime_time:
    try:
        parsed = datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError as exc:
        raise ValueError("时间必须为 HH:MM，例如 08:30") from exc
    return parsed


def _normalize_weekdays(values: list[int] | None) -> list[int]:
    days = sorted(set(range(7) if values is None else values))
    if not days or any(day < 0 or day > 6 for day in days):
        raise ValueError("weekdays 必须是 0（周一）到 6（周日）")
    return days


def _cron_values(token: str, low: int, high: int, *, sunday: bool = False) -> tuple[set[int], bool]:
    wildcard = token == "*"
    values: set[int] = set()
    for part in token.split(","):
        part = part.strip()
        if not part:
            raise ValueError("Cron 字段不能为空")
        base, slash, step_text = part.partition("/")
        step = int(step_text) if slash else 1
        if step <= 0:
            raise ValueError("Cron 步长必须大于 0")
        if base == "*":
            start, end = low, high
        elif "-" in base:
            left, right = base.split("-", 1)
            start, end = int(left), int(right)
        else:
            start = end = int(base)
        allowed_high = 7 if sunday else high
        if start < low or end > allowed_high or start > end:
            raise ValueError(f"Cron 字段超出范围 {low}-{allowed_high}：{part}")
        for number in range(start, end + 1, step):
            values.add(0 if sunday and number == 7 else number)
    return values, wildcard


def validate_cron(expression: str) -> str:
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("Cron 必须是五段：分 时 日 月 周")
    _cron_values(fields[0], 0, 59)
    _cron_values(fields[1], 0, 23)
    _cron_values(fields[2], 1, 31)
    _cron_values(fields[3], 1, 12)
    _cron_values(fields[4], 0, 6, sunday=True)
    return " ".join(fields)


def _cron_matches(local: datetime, expression: str) -> bool:
    minute, _ = _cron_values(expression.split()[0], 0, 59)
    hour, _ = _cron_values(expression.split()[1], 0, 23)
    day, day_any = _cron_values(expression.split()[2], 1, 31)
    month, _ = _cron_values(expression.split()[3], 1, 12)
    weekday, weekday_any = _cron_values(expression.split()[4], 0, 6, sunday=True)
    cron_weekday = (local.weekday() + 1) % 7
    if local.minute not in minute or local.hour not in hour or local.month not in month:
        return False
    day_match = local.day in day
    weekday_match = cron_weekday in weekday
    if not day_any and not weekday_any:
        return day_match or weekday_match
    return day_match and weekday_match


def normalize_schedule(schedule: dict, timezone_name: str, *, now: datetime | None = None) -> dict:
    now = (now or utc_now()).astimezone(UTC)
    zone = _zone(timezone_name)
    kind = str(schedule.get("kind") or "").lower()
    if kind == "once":
        raw = str(schedule.get("run_at") or "").strip()
        if not raw:
            raise ValueError("一次性任务缺少 run_at")
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=zone)
        run_at = parsed.astimezone(UTC)
        if run_at <= now:
            raise ValueError("一次性任务时间必须晚于当前时间")
        return {"kind": "once", "run_at": iso_utc(run_at)}
    if kind == "interval":
        seconds = int(schedule.get("every_seconds") or 0)
        if seconds < 10:
            raise ValueError("固定间隔不能小于 10 秒")
        anchor_raw = schedule.get("anchor_at")
        anchor = parse_iso(anchor_raw) if anchor_raw else now + timedelta(seconds=seconds)
        return {"kind": "interval", "every_seconds": seconds, "anchor_at": iso_utc(anchor)}
    if kind == "daily":
        clock = _parse_clock(str(schedule.get("time") or ""))
        weekdays = _normalize_weekdays(schedule.get("weekdays"))
        return {"kind": "daily", "time": clock.strftime("%H:%M"), "weekdays": weekdays}
    if kind == "cron":
        return {"kind": "cron", "expression": validate_cron(str(schedule.get("expression") or ""))}
    raise ValueError("调度类型必须是 once / interval / daily / cron")


def next_run(schedule: dict, timezone_name: str, *, after: datetime | None = None) -> datetime | None:
    after = (after or utc_now()).astimezone(UTC)
    zone = _zone(timezone_name)
    kind = schedule["kind"]
    if kind == "once":
        candidate = parse_iso(schedule["run_at"])
        return candidate if candidate > after else None
    if kind == "interval":
        anchor = parse_iso(schedule["anchor_at"])
        seconds = int(schedule["every_seconds"])
        if anchor > after:
            return anchor
        periods = math.floor((after - anchor).total_seconds() / seconds) + 1
        return anchor + timedelta(seconds=periods * seconds)
    if kind == "daily":
        clock = _parse_clock(schedule["time"])
        weekdays = set(schedule["weekdays"])
        local_after = after.astimezone(zone)
        for offset in range(8):
            date = local_after.date() + timedelta(days=offset)
            candidate = datetime.combine(date, clock, tzinfo=zone).astimezone(UTC)
            if date.weekday() in weekdays and candidate > after:
                return candidate
        raise RuntimeError("无法计算每日任务的下次时间")
    if kind == "cron":
        candidate = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = candidate + timedelta(days=370)
        while candidate <= limit:
            if _cron_matches(candidate.astimezone(zone), schedule["expression"]):
                return candidate
            candidate += timedelta(minutes=1)
        raise ValueError("Cron 在未来 370 天内没有可执行时间")
    raise ValueError(f"未知调度类型：{kind}")


def schedule_text(task: dict) -> str:
    schedule = task["schedule"]
    tz = task.get("timezone", DEFAULT_TIMEZONE)
    kind = schedule["kind"]
    if kind == "once":
        local = parse_iso(schedule["run_at"]).astimezone(_zone(tz))
        return f"一次性：{local:%Y-%m-%d %H:%M} ({tz})"
    if kind == "interval":
        seconds = int(schedule["every_seconds"])
        if seconds % 86400 == 0:
            interval = f"{seconds // 86400} 天"
        elif seconds % 3600 == 0:
            interval = f"{seconds // 3600} 小时"
        elif seconds % 60 == 0:
            interval = f"{seconds // 60} 分钟"
        else:
            interval = f"{seconds} 秒"
        return f"每 {interval}"
    if kind == "daily":
        labels = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        days = schedule["weekdays"]
        day_text = "每天" if days == list(range(7)) else "、".join(labels[i] for i in days)
        return f"{day_text} {schedule['time']} ({tz})"
    return f"Cron `{schedule['expression']}` ({tz})"


def local_time_text(task: dict, value: str | None) -> str:
    if not value:
        return "—"
    return parse_iso(value).astimezone(_zone(task.get("timezone", DEFAULT_TIMEZONE))).strftime("%Y-%m-%d %H:%M:%S")


def scheduler_health(*, max_age: float = 15.0, now: datetime | None = None) -> dict:
    payload: dict = {}
    try:
        with _connection() as conn:
            row = conn.execute(
                "SELECT status, pid, updated_at FROM scheduler_state WHERE singleton = 1"
            ).fetchone()
        if row:
            payload = dict(row)
    except sqlite3.Error:
        payload = {}
    if not payload:
        payload = _read(SCHEDULER_HEARTBEAT) or {}
    try:
        age = ((now or utc_now()).astimezone(UTC) - parse_iso(payload["updated_at"])).total_seconds()
    except (KeyError, TypeError, ValueError):
        age = float("inf")
    online = payload.get("status") == "running" and 0 <= age <= max_age
    return {"online": online, "age_seconds": age, "pid": payload.get("pid")}


def update_scheduler_health(status: str, pid: int, *, now: datetime | None = None) -> None:
    payload = {
        "status": status,
        "pid": int(pid),
        "updated_at": iso_utc((now or utc_now()).astimezone(UTC)),
    }
    with _transaction() as conn:
        cursor = conn.execute(
            """
            UPDATE scheduler_state
            SET status = ?, pid = ?, updated_at = ?
            WHERE singleton = 1
            """,
            (payload["status"], payload["pid"], payload["updated_at"]),
        )
        if not cursor.rowcount:
            conn.execute(
                """
                INSERT INTO scheduler_state (singleton, status, pid, updated_at)
                VALUES (1, ?, ?, ?)
                """,
                (payload["status"], payload["pid"], payload["updated_at"]),
            )

    # Keep the old heartbeat readable during a rolling restart of server/scheduler.
    SCHEDULER_HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
    SCHEDULER_HEARTBEAT.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def _new_id(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24] or "task"
    return f"{slug}-{uuid.uuid4().hex[:8]}"


def create_task(
    workdir: Path,
    chat_id: str,
    name: str,
    prompt: str,
    schedule: dict,
    *,
    timezone_name: str = DEFAULT_TIMEZONE,
    enabled: bool = True,
    now: datetime | None = None,
) -> dict:
    name = name.strip()
    prompt = prompt.strip()
    if not name or not prompt or not chat_id.strip():
        raise ValueError("name、prompt 和 chat_id 不能为空")
    now = (now or utc_now()).astimezone(UTC)
    normalized = normalize_schedule(schedule, timezone_name, now=now)
    next_at = next_run(normalized, timezone_name, after=now - timedelta(microseconds=1))
    task_id = _new_id(name)
    task = {
        "version": 2,
        "id": task_id,
        "chat_id": chat_id,
        "name": name,
        "prompt": prompt,
        "schedule": normalized,
        "timezone": timezone_name,
        "status": "enabled" if enabled else "paused",
        "created_at": iso_utc(now),
        "updated_at": iso_utc(now),
        "next_run_at": iso_utc(next_at),
        "last_run_at": None,
        "last_status": "never",
        "last_error": "",
        "run_count": 0,
        "manual_run_requested_at": None,
    }
    migrate_legacy_tasks(workdir)
    with _transaction() as conn:
        _save_task(conn, workdir, task)
    return task


def list_tasks(workdir: Path) -> list[dict]:
    migrate_legacy_tasks(workdir)
    with _connection() as conn:
        rows = conn.execute(
            "SELECT id, payload_json FROM tasks WHERE workdir = ?",
            (_workdir_key(workdir),),
        ).fetchall()
    tasks = [_task_from_row(row) for row in rows]
    return sorted(tasks, key=lambda item: (item.get("status") != "enabled", item.get("next_run_at") or "~", item.get("name") or ""))


def get_task(workdir: Path, task_id: str) -> dict | None:
    migrate_legacy_tasks(workdir)
    with _connection() as conn:
        return _task_from_row(_task_row(conn, workdir, task_id))


def resolve_task(workdir: Path, reference: str) -> dict:
    reference = reference.strip()
    tasks = list_tasks(workdir)
    exact = [task for task in tasks if task.get("id") == reference or task.get("name") == reference]
    if len(exact) == 1:
        return exact[0]
    prefix = [task for task in tasks if str(task.get("id", "")).startswith(reference)]
    if len(prefix) == 1:
        return prefix[0]
    if not exact and not prefix:
        raise ValueError(f"找不到定时任务：{reference}")
    raise ValueError(f"任务引用不唯一：{reference}")


def _mutate(workdir: Path, task_id: str, mutate) -> dict:
    migrate_legacy_tasks(workdir)
    with _transaction() as conn:
        task = _task_from_row(_task_row(conn, workdir, task_id))
        if task is None:
            raise ValueError(f"找不到定时任务：{task_id}")
        mutate(task)
        task["updated_at"] = iso_utc(utc_now())
        _save_task(conn, workdir, task)
        return task


def pause_task(workdir: Path, task_id: str) -> dict:
    return _mutate(workdir, task_id, lambda task: task.update(status="paused"))


def resume_task(workdir: Path, task_id: str) -> dict:
    def resume(task: dict) -> None:
        task["status"] = "enabled"
        task["next_run_at"] = iso_utc(next_run(task["schedule"], task["timezone"], after=utc_now()))
    return _mutate(workdir, task_id, resume)


def request_run(workdir: Path, task_id: str) -> dict:
    return _mutate(workdir, task_id, lambda task: task.update(manual_run_requested_at=iso_utc(utc_now())))


def update_task(
    workdir: Path,
    task_id: str,
    *,
    name: str | None = None,
    prompt: str | None = None,
    schedule: dict | None = None,
    timezone_name: str | None = None,
) -> dict:
    def update(task: dict) -> None:
        if name is not None:
            if not name.strip():
                raise ValueError("name 不能为空")
            task["name"] = name.strip()
        if prompt is not None:
            if not prompt.strip():
                raise ValueError("prompt 不能为空")
            task["prompt"] = prompt.strip()
        tz = timezone_name or task["timezone"]
        _zone(tz)
        if schedule is not None:
            task["schedule"] = normalize_schedule(schedule, tz)
        task["timezone"] = tz
        if schedule is not None or timezone_name is not None:
            task["next_run_at"] = iso_utc(next_run(task["schedule"], tz, after=utc_now()))
            if task["status"] == "completed":
                task["status"] = "enabled"
    return _mutate(workdir, task_id, update)


def delete_task(workdir: Path, task_id: str) -> None:
    migrate_legacy_tasks(workdir)
    with _transaction() as conn:
        task = _task_from_row(_task_row(conn, workdir, task_id))
        if task is None:
            raise ValueError(f"找不到定时任务：{task_id}")
        if task.get("last_status") == "running":
            raise ValueError("任务正在执行，请等本次完成后再删除")
        conn.execute(
            "DELETE FROM tasks WHERE workdir = ? AND id = ?",
            (_workdir_key(workdir), task_id),
        )
    shutil.rmtree(runs_dir(workdir, task_id), ignore_errors=True)


def claim_due_tasks(
    workdir: Path,
    *,
    now: datetime | None = None,
    limit: int | None = None,
) -> list[dict]:
    now = (now or utc_now()).astimezone(UTC)
    if limit is not None:
        limit = max(0, int(limit))
        if limit == 0:
            return []
    claimed: list[dict] = []
    migrate_legacy_tasks(workdir)
    with _transaction() as conn:
        rows = conn.execute(
            """
            SELECT id, payload_json
            FROM tasks
            WHERE workdir = ? AND last_status != 'running'
            ORDER BY id
            """,
            (_workdir_key(workdir),),
        ).fetchall()
        for row in rows:
            if limit is not None and len(claimed) >= limit:
                break
            task = _task_from_row(row)
            if not task or task.get("last_status") == "running":
                continue
            manual = bool(task.get("manual_run_requested_at"))
            scheduled_due = (
                task.get("status") == "enabled"
                and task.get("next_run_at")
                and parse_iso(task["next_run_at"]) <= now
            )
            if not manual and not scheduled_due:
                continue
            task["manual_run_requested_at"] = None
            task["last_status"] = "running"
            task["last_error"] = ""
            task["last_run_at"] = iso_utc(now)
            task["running_since"] = iso_utc(now)
            task["run_count"] = int(task.get("run_count") or 0) + 1
            task["updated_at"] = iso_utc(now)
            task["trigger"] = "scheduled" if scheduled_due else "manual"
            if scheduled_due:
                task["next_run_at"] = iso_utc(next_run(task["schedule"], task["timezone"], after=now))
            _save_task(conn, workdir, task)
            claimed.append(dict(task))
    return claimed


def finish_run(workdir: Path, task_id: str, status: str, *, error: str = "", output: str = "") -> dict:
    now = utc_now()
    migrate_legacy_tasks(workdir)
    with _transaction() as conn:
        task = _task_from_row(_task_row(conn, workdir, task_id))
        if task is None:
            raise ValueError(f"找不到定时任务：{task_id}")
        task["last_status"] = status
        task["last_error"] = error[:2000]
        task["last_finished_at"] = iso_utc(now)
        task["updated_at"] = iso_utc(now)
        task.pop("running_since", None)
        task.pop("trigger", None)
        if task["schedule"]["kind"] == "once" and not task.get("next_run_at"):
            task["status"] = "completed"
        _save_task(conn, workdir, task)
        conn.execute(
            """
            INSERT INTO task_runs (
                workdir, task_id, finished_at, status, error, output
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _workdir_key(workdir),
                task_id,
                iso_utc(now),
                status,
                error[:2000],
                output[:12000],
            ),
        )

    run_dir = runs_dir(workdir, task_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    (run_dir / f"{stamp}.json").write_text(json.dumps({
        "task_id": task_id,
        "finished_at": iso_utc(now),
        "status": status,
        "error": error[:2000],
        "output": output[:12000],
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return task


def list_runs(workdir: Path, task_id: str, *, limit: int = 50) -> list[dict]:
    migrate_legacy_tasks(workdir)
    with _connection() as conn:
        rows = conn.execute(
            """
            SELECT run_id, task_id, finished_at, status, error, output
            FROM task_runs
            WHERE workdir = ? AND task_id = ?
            ORDER BY run_id DESC
            LIMIT ?
            """,
            (_workdir_key(workdir), task_id, max(1, int(limit))),
        ).fetchall()
    return [dict(row) for row in rows]


def run_history_count(workdir: Path) -> int:
    with _connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM task_runs WHERE workdir = ?",
            (_workdir_key(workdir),),
        ).fetchone()
    return int(row["count"] if row else 0)


def clear_run_history(workdir: Path) -> int:
    with _transaction() as conn:
        cursor = conn.execute(
            "DELETE FROM task_runs WHERE workdir = ?",
            (_workdir_key(workdir),),
        )
    return max(0, int(cursor.rowcount))


def prune_run_history(workdir: Path, *, before: datetime) -> int:
    cutoff = iso_utc(before.astimezone(UTC))
    with _transaction() as conn:
        cursor = conn.execute(
            "DELETE FROM task_runs WHERE workdir = ? AND finished_at < ?",
            (_workdir_key(workdir), cutoff),
        )
    return max(0, int(cursor.rowcount))


def recover_interrupted(workdir: Path) -> int:
    recovered = 0
    migrate_legacy_tasks(workdir)
    with _transaction() as conn:
        rows = conn.execute(
            """
            SELECT id, payload_json
            FROM tasks
            WHERE workdir = ? AND last_status = 'running'
            """,
            (_workdir_key(workdir),),
        ).fetchall()
        for row in rows:
            task = _task_from_row(row)
            if not task or task.get("last_status") != "running":
                continue
            task["last_status"] = "interrupted"
            task["last_error"] = "调度进程重启，上一次执行状态无法确认"
            task["updated_at"] = iso_utc(utc_now())
            task.pop("running_since", None)
            task.pop("trigger", None)
            _save_task(conn, workdir, task)
            recovered += 1
    return recovered
