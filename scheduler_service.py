"""Persistent scheduler daemon for isolated per-conversation Codex jobs."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from utils import chatconfig, config, privacy, scheduled, workers


HERE = Path(__file__).resolve().parent
BOTS = HERE / "bots"
REGISTRY = HERE / "registry.json"
STATE = HERE / "state"
CONFIG = config.load()
config.install_log_redaction()
POLL_SECONDS = max(1.0, float(CONFIG.get("scheduler_poll_sec", 2)))
MAX_WORKERS = max(1, int(CONFIG.get("scheduler_max_workers", 2)))
TASK_TIMEOUT = max(60, int(CONFIG.get("scheduler_task_timeout_sec", 1800)))
_LOCK_FILE = None
_STOP = threading.Event()


def _acquire_single_instance() -> None:
    global _LOCK_FILE
    STATE.mkdir(exist_ok=True)
    _LOCK_FILE = (STATE / "scheduler.lock").open("w", encoding="utf-8")
    try:
        fcntl.flock(_LOCK_FILE, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("feishu-scheduler 已有一个实例在运行。")
        raise SystemExit(0)
    _LOCK_FILE.write(str(os.getpid()))
    _LOCK_FILE.flush()


def _workdirs() -> list[Path]:
    try:
        chats = json.loads(REGISTRY.read_text(encoding="utf-8")).get("chats", {})
    except (OSError, json.JSONDecodeError):
        return []
    root = BOTS.resolve()
    result = set()
    for relative in chats.values():
        workdir = (BOTS / str(relative)).resolve()
        if workdir == root or root not in workdir.parents or not workdir.is_dir():
            continue
        result.add(workdir)
    return sorted(result)


def _scheduled_prompt(task: dict) -> str:
    return (
        "[定时任务系统提示]\n"
        f"任务名称：{task['name']}\n"
        f"任务 ID：{task['id']}\n"
        f"触发时间：{datetime.now(timezone.utc).isoformat()}\n\n"
        "这是无人值守的独立执行。不要提问或等待用户回复；信息不足时做合理假设并在结果中说明。\n"
        "如需发送文件或图片，可使用 feishu-send Skill。不要主动发送普通终稿文字，"
        "调度器会把你的最终回复自动发到当前飞书对话。\n"
        "最终回复应包含有用的结论、摘要、风险或注意事项。\n\n"
        f"[任务内容]\n{task['prompt']}"
    )


def _codex_command(workdir: Path, output_path: Path) -> list[str]:
    cfg = chatconfig.get(workdir)
    model = cfg.get("codex_model", chatconfig.DEFAULTS["codex_model"])
    effort = chatconfig.normalize_codex_effort(
        model, cfg.get("codex_effort", chatconfig.DEFAULTS["codex_effort"])
    )
    return [
        shutil.which("codex") or "codex",
        "exec",
        "--cd", str(workdir),
        "--skip-git-repo-check",
        "--ephemeral",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--model", str(model),
        "-c", f"model_reasoning_effort={effort}",
        "--output-last-message", str(output_path),
        "-",
    ]


def _write_log(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", errors="replace")


def run_task(workdir: Path, task: dict) -> None:
    privacy.cleanup_if_due(workdir)
    task_id = task["id"]
    run_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = scheduled.runs_dir(workdir, task_id) / f"{run_stamp}-{int(task.get('run_count') or 0)}"
    run_dir.mkdir(parents=True, exist_ok=True)
    output_path = run_dir / "final.txt"
    env = dict(os.environ)
    env.update({
        "CHAT_ID": str(task["chat_id"]),
        "CHAT_AGENT_HOME": str(HERE),
        "CHAT_AGENT_SCHEDULED_TASK_ID": task_id,
        "CHAT_AGENT_NO_SENT_MARKER": "1",
    })
    print(f"[scheduler] start chat={task['chat_id']} task={task_id} name={task['name']!r}")
    try:
        completed = subprocess.run(
            _codex_command(workdir, output_path),
            input=_scheduled_prompt(task),
            text=True,
            capture_output=True,
            cwd=workdir,
            env=env,
            timeout=TASK_TIMEOUT,
        )
        _write_log(run_dir / "stdout.log", completed.stdout)
        _write_log(run_dir / "stderr.log", completed.stderr)
        answer = output_path.read_text(encoding="utf-8").strip() if output_path.is_file() else ""
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or f"Codex exit {completed.returncode}").strip()[-1200:]
            raise RuntimeError(detail)
        if not answer:
            raise RuntimeError("Codex 执行完成，但没有生成最终回复")
        workers.deliver_new(task["chat_id"], f"⏰ **定时任务：{task['name']}**\n\n{answer}", "md")
        scheduled.finish_run(workdir, task_id, "success", output=answer)
        print(f"[scheduler] success task={task_id} len={len(answer)}")
    except subprocess.TimeoutExpired as exc:
        _write_log(run_dir / "stdout.log", exc.stdout or "")
        _write_log(run_dir / "stderr.log", exc.stderr or "")
        error = f"执行超过 {TASK_TIMEOUT} 秒，已终止"
        scheduled.finish_run(workdir, task_id, "failed", error=error)
        workers.deliver_new(task["chat_id"], f"❌ 定时任务「{task['name']}」失败：{error}", "md")
        print(f"[scheduler] timeout task={task_id}")
    except Exception as exc:
        error = config.redact_text(f"{exc.__class__.__name__}: {exc}")
        scheduled.finish_run(workdir, task_id, "failed", error=error)
        try:
            workers.deliver_new(task["chat_id"], f"❌ 定时任务「{task['name']}」失败：{error[:900]}", "md")
        except Exception as send_exc:
            print(f"[scheduler] failure notice failed task={task_id}: {send_exc}")
        print(f"[scheduler] failed task={task_id}: {error}")


def _stop(*_args) -> None:
    _STOP.set()


def _heartbeat(status: str = "running") -> None:
    scheduled.update_scheduler_health(status, os.getpid())


def main() -> None:
    _acquire_single_instance()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    recovered: set[Path] = set()
    futures: set[Future] = set()
    workdir_cursor = 0
    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="feishu-scheduled-task")
    print(f"feishu-scheduler 启动：poll={POLL_SECONDS}s workers={MAX_WORKERS} timeout={TASK_TIMEOUT}s")
    try:
        while not _STOP.is_set():
            _heartbeat()
            completed = {future for future in futures if future.done()}
            for future in completed:
                try:
                    future.result()
                except Exception as exc:
                    print(f"[scheduler] worker crashed unexpectedly: {config.redact_text(exc)}")
            futures -= completed

            workdirs = _workdirs()
            for workdir in workdirs:
                if workdir not in recovered:
                    count = scheduled.recover_interrupted(workdir)
                    if count:
                        print(f"[scheduler] recovered={count} workdir={workdir.name}")
                    recovered.add(workdir)

            available = MAX_WORKERS - len(futures)
            if workdirs and available > 0:
                offset = workdir_cursor % len(workdirs)
                ordered_workdirs = workdirs[offset:] + workdirs[:offset]
                workdir_cursor = (offset + 1) % len(workdirs)
                for workdir in ordered_workdirs:
                    claimed = scheduled.claim_due_tasks(workdir, limit=available)
                    for task in claimed:
                        futures.add(executor.submit(run_task, workdir, task))
                    available -= len(claimed)
                    if available <= 0:
                        break
            _STOP.wait(POLL_SECONDS)
    finally:
        _heartbeat("stopped")
        executor.shutdown(wait=False, cancel_futures=True)
        print("feishu-scheduler 已停止")


if __name__ == "__main__":
    main()
