from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import scheduler_service
from utils import panel, scheduled


UTC = timezone.utc


class IsolatedSchedulerDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._scheduler_state = tempfile.TemporaryDirectory()
        state = Path(self._scheduler_state.name)
        self._db_patch = patch.object(scheduled, "SCHEDULER_DB", state / "scheduler.db")
        self._heartbeat_patch = patch.object(
            scheduled, "SCHEDULER_HEARTBEAT", state / "scheduler.heartbeat.json"
        )
        self._db_patch.start()
        self._heartbeat_patch.start()

    def tearDown(self) -> None:
        self._heartbeat_patch.stop()
        self._db_patch.stop()
        self._scheduler_state.cleanup()


class ScheduleCalculationTests(unittest.TestCase):
    def test_interval_starts_after_one_full_interval(self) -> None:
        now = datetime(2026, 7, 21, 0, 0, tzinfo=UTC)
        value = scheduled.normalize_schedule(
            {"kind": "interval", "every_seconds": 3600}, "Asia/Shanghai", now=now
        )

        self.assertEqual(scheduled.next_run(value, "Asia/Shanghai", after=now), now + timedelta(hours=1))

    def test_daily_weekdays_skips_weekend(self) -> None:
        # 2026-07-24 is Friday; 01:00 UTC is 09:00 Asia/Shanghai.
        friday_after_run = datetime(2026, 7, 24, 2, 0, tzinfo=UTC)
        schedule = scheduled.normalize_schedule(
            {"kind": "daily", "time": "09:00", "weekdays": [0, 1, 2, 3, 4]},
            "Asia/Shanghai",
            now=friday_after_run,
        )

        self.assertEqual(
            scheduled.next_run(schedule, "Asia/Shanghai", after=friday_after_run),
            datetime(2026, 7, 27, 1, 0, tzinfo=UTC),
        )

    def test_cron_supports_monthly_schedule(self) -> None:
        after = datetime(2026, 7, 21, 0, 0, tzinfo=UTC)
        schedule = scheduled.normalize_schedule(
            {"kind": "cron", "expression": "0 9 1 * *"}, "Asia/Shanghai", now=after
        )

        self.assertEqual(
            scheduled.next_run(schedule, "Asia/Shanghai", after=after),
            datetime(2026, 8, 1, 1, 0, tzinfo=UTC),
        )


class ScheduledTaskStoreTests(IsolatedSchedulerDatabaseTest):
    def test_due_task_is_claimed_once_and_persists_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            now = datetime(2026, 7, 21, 0, 0, tzinfo=UTC)
            task = scheduled.create_task(
                workdir,
                "oc_test",
                "daily report",
                "Create the daily report",
                {"kind": "daily", "time": "08:00", "weekdays": list(range(7))},
                now=now,
            )
            scheduled.request_run(workdir, task["id"])

            claimed = scheduled.claim_due_tasks(workdir, now=now)
            claimed_again = scheduled.claim_due_tasks(workdir, now=now)

            self.assertEqual([item["id"] for item in claimed], [task["id"]])
            self.assertEqual(claimed_again, [])
            scheduled.finish_run(workdir, task["id"], "success", output="done")
            stored = scheduled.get_task(workdir, task["id"])
            self.assertEqual(stored["last_status"], "success")
            self.assertEqual(stored["run_count"], 1)
            self.assertEqual(scheduled.list_runs(workdir, task["id"])[0]["output"], "done")

            with sqlite3.connect(scheduled.database_path()) as conn:
                self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_manual_run_while_running_queues_exactly_one_more_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            task = scheduled.create_task(
                workdir,
                "oc_test",
                "check",
                "Check status",
                {"kind": "interval", "every_seconds": 3600},
            )
            scheduled.request_run(workdir, task["id"])
            first = scheduled.claim_due_tasks(workdir)
            scheduled.request_run(workdir, task["id"])

            self.assertEqual(len(first), 1)
            self.assertEqual(scheduled.claim_due_tasks(workdir), [])
            scheduled.finish_run(workdir, task["id"], "success")
            self.assertEqual(len(scheduled.claim_due_tasks(workdir)), 1)

    def test_concurrent_claim_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            task = scheduled.create_task(
                workdir,
                "oc_test",
                "atomic",
                "Only run once",
                {"kind": "interval", "every_seconds": 3600},
            )
            scheduled.request_run(workdir, task["id"])

            with ThreadPoolExecutor(max_workers=2) as executor:
                claims = list(executor.map(lambda _item: scheduled.claim_due_tasks(workdir), range(2)))

            self.assertEqual(sum(len(items) for items in claims), 1)

    def test_claim_limit_does_not_mark_queued_work_as_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            task_ids = []
            for index in range(3):
                task = scheduled.create_task(
                    workdir,
                    "oc_test",
                    f"limited-{index}",
                    "Run limited task",
                    {"kind": "interval", "every_seconds": 3600},
                )
                task_ids.append(task["id"])
                scheduled.request_run(workdir, task["id"])

            first = scheduled.claim_due_tasks(workdir, limit=2)
            self.assertEqual(len(first), 2)
            still_pending = [
                task
                for task_id in task_ids
                if (task := scheduled.get_task(workdir, task_id))["last_status"] != "running"
            ]
            self.assertEqual(len(still_pending), 1)

            for task in first:
                scheduled.finish_run(workdir, task["id"], "success")
            self.assertEqual(len(scheduled.claim_due_tasks(workdir, limit=2)), 1)

    def test_legacy_json_is_imported_once_and_not_resurrected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            legacy_dir = workdir / "scheduled_tasks" / "tasks"
            legacy_dir.mkdir(parents=True)
            legacy = {
                "version": 1,
                "id": "legacy-task",
                "chat_id": "oc_legacy",
                "name": "legacy",
                "prompt": "legacy prompt",
                "schedule": {
                    "kind": "daily",
                    "time": "08:00",
                    "weekdays": list(range(7)),
                },
                "timezone": "Asia/Shanghai",
                "status": "enabled",
                "created_at": "2026-07-21T00:00:00Z",
                "updated_at": "2026-07-21T00:00:00Z",
                "next_run_at": "2026-07-22T00:00:00Z",
                "last_run_at": None,
                "last_status": "never",
                "last_error": "",
                "run_count": 0,
                "manual_run_requested_at": None,
            }
            (legacy_dir / "legacy-task.json").write_text(
                json.dumps(legacy, ensure_ascii=False),
                encoding="utf-8",
            )

            migration = scheduled.migrate_legacy_tasks(workdir)
            self.assertEqual(migration["imported"], 1)
            self.assertEqual(scheduled.get_task(workdir, "legacy-task")["name"], "legacy")

            legacy["updated_at"] = "2026-07-21T01:00:00Z"
            legacy["last_status"] = "success"
            (legacy_dir / "legacy-task.json").write_text(
                json.dumps(legacy, ensure_ascii=False),
                encoding="utf-8",
            )
            refresh = scheduled.migrate_legacy_tasks(workdir)
            self.assertEqual(refresh["refreshed"], 1)
            self.assertEqual(scheduled.get_task(workdir, "legacy-task")["last_status"], "success")

            scheduled.pause_task(workdir, "legacy-task")
            legacy["updated_at"] = "2099-07-21T02:00:00Z"
            legacy["status"] = "enabled"
            (legacy_dir / "legacy-task.json").write_text(
                json.dumps(legacy, ensure_ascii=False),
                encoding="utf-8",
            )
            scheduled.migrate_legacy_tasks(workdir)
            self.assertEqual(scheduled.get_task(workdir, "legacy-task")["status"], "paused")

            scheduled.delete_task(workdir, "legacy-task")
            self.assertIsNone(scheduled.get_task(workdir, "legacy-task"))
            self.assertTrue((legacy_dir / "legacy-task.json").is_file())

    def test_control_panel_shows_task_details(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            task = scheduled.create_task(
                workdir,
                "oc_test",
                "AI news",
                "Summarize AI news",
                {"kind": "daily", "time": "08:30", "weekdays": [0, 1, 2, 3, 4]},
            )

            rendered = json.dumps(panel.schedule_card(workdir, task["id"]), ensure_ascii=False)

            self.assertIn("AI news", rendered)
            self.assertIn("Summarize AI news", rendered)
            self.assertIn("立即运行", rendered)
            self.assertIn("下次", rendered)


class SchedulerExecutionTests(IsolatedSchedulerDatabaseTest):
    def test_scheduled_codex_runs_without_approval_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            output = workdir / "final.txt"

            command = scheduler_service._codex_command(workdir, output)

            self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
            self.assertIn("--dangerously-bypass-hook-trust", command)

    def test_isolated_codex_result_is_delivered_and_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            (workdir / "agent.json").write_text(
                json.dumps({"codex_model": "gpt-5", "codex_effort": "high"}),
                encoding="utf-8",
            )
            task = scheduled.create_task(
                workdir,
                "oc_test",
                "report",
                "Create report",
                {"kind": "interval", "every_seconds": 3600},
            )
            scheduled.request_run(workdir, task["id"])
            claimed = scheduled.claim_due_tasks(workdir)[0]

            def fake_run(command, **_kwargs):
                output = Path(command[command.index("--output-last-message") + 1])
                output.write_text("核心结论：一切正常。", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="events", stderr="")

            with patch.object(scheduler_service.subprocess, "run", side_effect=fake_run), patch.object(
                scheduler_service.workers, "deliver_new"
            ) as deliver:
                scheduler_service.run_task(workdir, claimed)

            command = scheduler_service._codex_command(workdir, Path(tmp) / "out.txt")
            self.assertIn("--ephemeral", command)
            self.assertIn("gpt-5", command)
            deliver.assert_called_once()
            self.assertIn("定时任务：report", deliver.call_args.args[1])
            self.assertEqual(scheduled.get_task(workdir, task["id"])["last_status"], "success")


if __name__ == "__main__":
    unittest.main()
