from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from utils import hub, panel, workers
from utils.resource_lifecycle import MCP_BUNDLE_TOKEN, scaffold_mcp, scaffold_skill


def _finish_skill(path: Path) -> None:
    skill_md = path / "SKILL.md"
    skill_md.write_text(
        skill_md.read_text(encoding="utf-8").replace(
            "TODO: Replace this line with concise, imperative instructions and add only the reusable resources required.",
            "Read the user request, perform the reusable workflow, and verify the result.",
        ),
        encoding="utf-8",
    )


class SkillLifecycleTests(unittest.TestCase):
    def test_scaffold_creates_codex_only_dedicated_skill(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            path = scaffold_skill(workdir, "summarize-research", "Summarize research notes when asked.")

            self.assertEqual(path, workdir / ".codex" / "skills" / "summarize-research")
            self.assertTrue((path / "SKILL.md").is_file())
            self.assertTrue((path / "VERSION").is_file())
            self.assertTrue((path / "agents" / "openai.yaml").is_file())
            self.assertFalse(hub.validate_skill(workdir, "summarize-research").ok)

            _finish_skill(path)
            self.assertTrue(hub.validate_skill(workdir, "summarize-research").ok)

    def test_publish_validates_generalization_and_version_bump(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "bot"
            skills = root / "hub" / "skills"
            skills.mkdir(parents=True)
            path = scaffold_skill(workdir, "research-summary", "Summarize reusable research material.")
            _finish_skill(path)

            with patch.object(hub, "SKILLS", skills):
                first = hub.publish_skill(workdir, "research-summary")
                same_version = hub.publish_skill(workdir, "research-summary", overwrite=True)
                (path / "VERSION").write_text("0.2.0\n", encoding="utf-8")
                upgraded = hub.publish_skill(workdir, "research-summary", overwrite=True)

            self.assertTrue(first.ok)
            self.assertFalse(same_version.ok)
            self.assertIn("提升 VERSION", " ".join(same_version.errors))
            self.assertTrue(upgraded.ok)

    def test_publish_rejects_private_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp) / "bot"
            path = scaffold_skill(workdir, "private-helper", "Use a private helper when asked.")
            _finish_skill(path)
            (path / "references").mkdir()
            (path / "references" / "config.md").write_text("Read /Users/example/private/data.json", encoding="utf-8")

            report = hub.validate_skill(workdir, "private-helper", publish=True)

            self.assertFalse(report.ok)
            self.assertIn("用户绝对路径", " ".join(report.errors))

    def test_system_defaults_refresh_codex_copy_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub_root = root / "hub"
            skills = hub_root / "skills"
            mcp_root = hub_root / "mcp"
            source = skills / "system-helper"
            source.mkdir(parents=True)
            mcp_root.mkdir()
            (source / "SKILL.md").write_text("---\nname: system-helper\ndescription: System helper.\n---\n\n# New\n", encoding="utf-8")
            (source / "VERSION").write_text("1.0.0\n", encoding="utf-8")
            (hub_root / "defaults.json").write_text(json.dumps({"skills": ["system-helper"], "mcp": []}), encoding="utf-8")
            workdir = root / "bot"
            stale = workdir / ".codex" / "skills" / "system-helper"
            stale.mkdir(parents=True)
            (stale / "SKILL.md").write_text("stale", encoding="utf-8")

            with patch.object(hub, "HUB", hub_root), patch.object(hub, "SKILLS", skills), patch.object(hub, "MCP", mcp_root):
                hub.apply_defaults(workdir)

            self.assertIn("# New", (stale / "SKILL.md").read_text(encoding="utf-8"))

    def test_general_resource_can_be_promoted_to_system_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            hub_root = Path(tmp) / "hub"
            skills = hub_root / "skills"
            mcp_root = hub_root / "mcp"
            (skills / "shared-helper").mkdir(parents=True)
            mcp_root.mkdir(parents=True)
            (hub_root / "defaults.json").write_text(json.dumps({"skills": [], "mcp": []}), encoding="utf-8")

            with patch.object(hub, "HUB", hub_root), patch.object(hub, "SKILLS", skills), patch.object(hub, "MCP", mcp_root):
                changed = hub.set_system_default("skill", "shared-helper")
                unchanged = hub.set_system_default("skill", "shared-helper")

            stored = json.loads((hub_root / "defaults.json").read_text(encoding="utf-8"))
            self.assertTrue(changed)
            self.assertFalse(unchanged)
            self.assertEqual(stored["skills"], ["shared-helper"])


class McpLifecycleTests(unittest.TestCase):
    def test_grouped_config_mcp_can_publish_under_library_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "bot"
            bundle = workdir / ".codex" / "mcp" / "market-data"
            bundle.mkdir(parents=True)
            (bundle / "mcp.json").write_text(json.dumps({
                "market-stocks": {"url": "https://example.com/stocks/mcp"},
                "market-news": {"url": "https://example.com/news/mcp"},
            }), encoding="utf-8")
            mcp_root = root / "hub" / "mcp"
            mcp_root.mkdir(parents=True)

            validated = hub.validate_mcp(workdir, "market-data", publish=True)
            with patch.object(hub, "MCP", mcp_root):
                published = hub.publish_mcp(workdir, "market-data")

            self.assertTrue(validated.ok)
            self.assertTrue(published.ok)
            document = json.loads(
                (mcp_root / "market-data.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                sorted(document),
                ["market-news", "market-stocks"],
            )

    def test_generated_bundle_registers_and_publishes_portably(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "bot"
            mcp_root = root / "hub" / "mcp"
            mcp_root.mkdir(parents=True)
            bundle = scaffold_mcp(workdir, "research-tools", command="python3", bundle=True)
            (bundle / "server.py").write_text("print('server')\n", encoding="utf-8")
            (bundle / "mcp.json").write_text(
                json.dumps({
                    "research-tools": {
                        "command": "python3",
                        "args": [f"{MCP_BUNDLE_TOKEN}/server.py"],
                        "startup_timeout_sec": 30,
                        "tool_timeout_sec": 120,
                    }
                }),
                encoding="utf-8",
            )

            registered = hub.register_local_mcp(workdir, "research-tools")
            config = (workdir / ".codex" / "config.toml").read_text(encoding="utf-8")
            active = json.loads((workdir / ".mcp.json").read_text(encoding="utf-8"))
            with patch.object(hub, "MCP", mcp_root):
                published = hub.publish_mcp(workdir, "research-tools")

            self.assertTrue(registered.ok)
            self.assertIn(str((bundle / "server.py").resolve()), active["mcpServers"]["research-tools"]["args"])
            self.assertIn("startup_timeout_sec = 30", config)
            self.assertIn("tool_timeout_sec = 120", config)
            self.assertTrue(published.ok)
            self.assertTrue((mcp_root / "research-tools" / "server.py").is_file())
            published_doc = (mcp_root / "research-tools" / "mcp.json").read_text(encoding="utf-8")
            self.assertIn(MCP_BUNDLE_TOKEN, published_doc)

    def test_local_shared_mcp_allows_plaintext_secret_with_warning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            (workdir / ".mcp.json").write_text(json.dumps({
                "mcpServers": {
                    "private-api": {
                        "url": "https://example.com/mcp",
                        "headers": {"Authorization": "Bearer real-secret-value"},
                    }
                }
            }), encoding="utf-8")

            report = hub.validate_mcp(workdir, "private-api", publish=True)

            self.assertTrue(report.ok)
            self.assertIn("明文凭据", " ".join(report.warnings))

    def test_http_mcp_uses_config_only_format_without_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)

            path = scaffold_mcp(workdir, "remote-search", url="https://example.com/mcp")
            document = json.loads(path.read_text(encoding="utf-8"))
            document["mcpServers"]["remote-search"]["headers"] = {
                "Authorization": "Bearer local-secret"
            }
            path.write_text(json.dumps(document), encoding="utf-8")
            registered = hub.register_local_mcp(workdir, "remote-search")

            self.assertEqual(path, workdir / ".mcp.json")
            self.assertFalse((workdir / ".codex" / "mcp" / "remote-search").exists())
            self.assertTrue(registered.ok)
            self.assertEqual(registered.details["storage"], "config")
            codex_config = (workdir / ".codex" / "config.toml").read_text(encoding="utf-8")
            self.assertIn("[mcp_servers.remote-search]", codex_config)
            self.assertIn("[mcp_servers.remote-search.http_headers]", codex_config)
            self.assertNotIn("[mcp_servers.remote-search.headers]", codex_config)

    def test_legacy_url_bundle_is_published_as_single_config_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workdir = root / "bot"
            bundle = workdir / ".codex" / "mcp" / "remote-api"
            bundle.mkdir(parents=True)
            (bundle / "VERSION").write_text("0.1.0\n", encoding="utf-8")
            (bundle / "mcp.json").write_text(json.dumps({
                "remote-api": {
                    "url": "https://example.com/mcp",
                    "bearer_token_env_var": "REMOTE_API_TOKEN",
                }
            }), encoding="utf-8")
            (workdir / ".mcp.json").write_text(json.dumps({
                "mcpServers": {
                    "remote-api": {
                        "url": "https://example.com/mcp",
                        "headers": {"Authorization": "Bearer local-shared-secret"},
                    }
                }
            }), encoding="utf-8")
            mcp_root = root / "hub" / "mcp"
            old_hub_bundle = mcp_root / "remote-api"
            old_hub_bundle.mkdir(parents=True)
            (old_hub_bundle / "VERSION").write_text("0.1.0\n", encoding="utf-8")
            (old_hub_bundle / "mcp.json").write_text("{}", encoding="utf-8")

            with patch.object(hub, "MCP", mcp_root):
                report = hub.publish_mcp(workdir, "remote-api", overwrite=True)

            published = json.loads((mcp_root / "remote-api.json").read_text(encoding="utf-8"))
            self.assertTrue(report.ok)
            self.assertFalse((mcp_root / "remote-api").exists())
            self.assertFalse(bundle.exists())
            self.assertEqual(
                published["remote-api"]["headers"]["Authorization"],
                "Bearer local-shared-secret",
            )

    def test_reconcile_updates_loaded_config_mcp_and_removes_old_server_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub_root = root / "hub"
            mcp_root = hub_root / "mcp"
            skills_root = hub_root / "skills"
            mcp_root.mkdir(parents=True)
            skills_root.mkdir()
            (hub_root / "defaults.json").write_text(
                json.dumps({"skills": [], "mcp": []}), encoding="utf-8"
            )
            source = mcp_root / "shared-notes.json"
            source.write_text(json.dumps({
                "old-notes": {"url": "http://127.0.0.1:29999/mcp/"}
            }), encoding="utf-8")
            workdir = root / "bot"
            workdir.mkdir()

            with patch.object(hub, "HUB", hub_root), patch.object(
                hub, "MCP", mcp_root
            ), patch.object(hub, "SKILLS", skills_root):
                hub.load_mcp(workdir, "shared-notes")
                first = json.loads((workdir / hub.RESOURCE_STATE).read_text(encoding="utf-8"))
                source.write_text(json.dumps({
                    "new-notes": {"url": "http://127.0.0.1:29999/mcp/"}
                }), encoding="utf-8")

                changes = hub.reconcile_loaded_resources(workdir)
                active = json.loads((workdir / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
                second = json.loads((workdir / hub.RESOURCE_STATE).read_text(encoding="utf-8"))
                meta = hub.mcp_meta("shared-notes")

            self.assertEqual(changes, ["mcp:shared-notes"])
            self.assertIn("old-notes", first["mcp"]["shared-notes"]["keys"])
            self.assertNotIn("old-notes", active)
            self.assertIn("new-notes", active)
            self.assertEqual(second["mcp"]["shared-notes"]["keys"], ["new-notes"])
            self.assertTrue(meta["version"].startswith("rev-"))

    def test_reconcile_does_not_install_unselected_general_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hub_root = root / "hub"
            mcp_root = hub_root / "mcp"
            skills_root = hub_root / "skills"
            mcp_root.mkdir(parents=True)
            skills_root.mkdir()
            (hub_root / "defaults.json").write_text(
                json.dumps({"skills": [], "mcp": []}), encoding="utf-8"
            )
            (mcp_root / "optional.json").write_text(json.dumps({
                "optional": {"url": "https://example.com/mcp"}
            }), encoding="utf-8")
            workdir = root / "bot"
            workdir.mkdir()

            with patch.object(hub, "HUB", hub_root), patch.object(
                hub, "MCP", mcp_root
            ), patch.object(hub, "SKILLS", skills_root):
                changes = hub.reconcile_loaded_resources(workdir)

            self.assertEqual(changes, [])
            self.assertFalse((workdir / ".mcp.json").exists())


class ResourceUpdateNoticeTests(unittest.TestCase):
    def test_resource_updates_are_split_by_reload_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            hub.mark_resource_updates(workdir, ["skill:summary", "mcp:search"])

            pending = hub.pending_resource_updates(workdir)
            self.assertEqual(pending["skills"], ["summary"])
            self.assertEqual(pending["mcp"], ["search"])

            hub.dismiss_skill_updates(workdir)
            pending = hub.pending_resource_updates(workdir)
            self.assertEqual(pending["skills"], [])
            self.assertEqual(pending["mcp"], ["search"])

            hub.clear_resource_updates(workdir)
            self.assertEqual(hub.pending_resource_updates(workdir)["mcp"], [])

    def test_control_panel_distinguishes_dynamic_skill_and_pending_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            hub.mark_resource_updates(workdir, ["skill:summary", "mcp:search"])

            rendered = json.dumps(panel.main_menu_card(workdir, "oc_test"), ensure_ascii=False)

            self.assertIn("Codex 会自动检测，通常无需重启", rendered)
            self.assertIn("当前会话尚未重新载入", rendered)
            self.assertIn("重启当前会话并载入", rendered)
            self.assertNotIn("重启整个", rendered)

    def test_periodic_hub_sync_marks_pending_without_restarting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bots = Path(tmp) / "bots"
            workdir = bots / "bot-one"
            workdir.mkdir(parents=True)
            registry = {"chats": {"oc_test": "bot-one"}}
            with patch.object(server, "BOTS", bots), patch.object(
                server, "load_registry", return_value=registry
            ), patch.object(
                server.hub, "reconcile_loaded_resources",
                return_value=["skill:summary", "mcp:shared-notes"],
            ), patch.object(
                server.session, "exists", return_value=True
            ), patch.object(server, "_restart_and_notify") as restart:
                result = server.reconcile_hub_resources_once()

            self.assertEqual(result, {"updated": 1, "pending": 1})
            pending = hub.pending_resource_updates(workdir)
            self.assertEqual(pending["skills"], ["summary"])
            self.assertEqual(pending["mcp"], ["shared-notes"])
            restart.assert_not_called()

    def test_periodic_hub_sync_does_not_leave_notice_for_stopped_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bots = Path(tmp) / "bots"
            workdir = bots / "bot-one"
            workdir.mkdir(parents=True)
            registry = {"chats": {"oc_test": "bot-one"}}
            with patch.object(server, "BOTS", bots), patch.object(
                server, "load_registry", return_value=registry
            ), patch.object(
                server.hub, "reconcile_loaded_resources", return_value=["mcp:shared-notes"]
            ), patch.object(
                server.session, "exists", return_value=False
            ), patch.object(server, "_restart_and_notify") as restart:
                result = server.reconcile_hub_resources_once()

            self.assertEqual(result, {"updated": 1, "pending": 0})
            self.assertEqual(hub.pending_resource_updates(workdir)["mcp"], [])
            restart.assert_not_called()

    def test_user_confirmed_restart_clears_resource_notice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            server.session, "restart", return_value=True
        ) as restart, patch.object(
            server.session, "codex_startup_warning", return_value=""
        ), patch.object(server.feishu, "send_text") as send_text:
            workdir = Path(tmp)
            hub.mark_resource_updates(workdir, ["mcp:search"])

            server._restart_and_notify("oc_test", workdir, "用户确认")

            restart.assert_called_once_with("oc_test", workdir)
            send_text.assert_called_once()
            self.assertIn("当前 Codex 会话已重启", send_text.call_args.args[1])
            self.assertEqual(hub.pending_resource_updates(workdir)["mcp"], [])


class FeishuDeliveryTests(unittest.TestCase):
    def test_sent_file_does_not_replace_useful_final_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            workdir = Path(tmp) / "bot"
            workdir.mkdir()
            with patch.object(workers, "STATE", state), patch.object(
                workers, "deliver_with_fallback"
            ) as deliver:
                workers.final_path("oc_test").write_text(
                    "报告已发送。核心结论：收入增长，但现金流风险需要优先关注。", encoding="utf-8"
                )
                workers.sent_path("oc_test").write_text("已发送文件：report.pdf", encoding="utf-8")

                workers.progress_worker("oc_test", "message-id", workdir)

            delivered = deliver.call_args.args[2]
            self.assertIn("核心结论", delivered)
            self.assertIn("现金流风险", delivered)
            self.assertFalse(workers.sent_path("oc_test").exists())

    def test_sent_confirmation_is_used_only_when_final_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            workdir = Path(tmp) / "bot"
            workdir.mkdir()
            with patch.object(workers, "STATE", state), patch.object(
                workers, "deliver_with_fallback"
            ) as deliver:
                workers.final_path("oc_test").write_text("", encoding="utf-8")
                workers.sent_path("oc_test").write_text("已发送文件：report.pdf", encoding="utf-8")

                workers.progress_worker("oc_test", "message-id", workdir)

            self.assertEqual(deliver.call_args.args[2], "已发送文件：report.pdf")


class CodexReplyCaptureTests(unittest.TestCase):
    def test_final_answer_events_are_recognized_before_task_complete(self) -> None:
        event = {
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "phase": "final_answer",
                "message": "这是完整终稿。",
            },
        }

        self.assertEqual(workers._final_event_message(event), "这是完整终稿。")

    def test_worker_switches_from_cached_baseline_to_new_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir()
            workdir = root / "bot"
            workdir.mkdir()
            old = root / "rollout-old.jsonl"
            new = root / "rollout-new.jsonl"
            old.write_text('{"type":"session_meta"}\n', encoding="utf-8")
            new.write_text(
                json.dumps({
                    "type": "event_msg",
                    "payload": {
                        "type": "agent_message",
                        "phase": "final_answer",
                        "message": "新 rollout 的终稿",
                    },
                }, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            with patch.object(workers, "STATE", state), patch.object(
                workers.session, "latest_codex_session", return_value=new
            ):
                workers.codex_reply_worker(
                    "oc_test",
                    workdir,
                    (old, 0, old.stat().st_size),
                )
                self.assertEqual(
                    workers.final_path("oc_test").read_text(encoding="utf-8"),
                    "新 rollout 的终稿",
                )


class ProgressContinuityTests(unittest.TestCase):
    def test_starting_card_uses_original_turn_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            workdir = Path(tmp) / "bot"
            workdir.mkdir()
            with patch.object(workers, "STATE", state), patch.object(
                workers.time, "time", return_value=112.0
            ), patch.object(workers.time, "sleep"), patch.object(
                workers, "_active_size", return_value=0
            ), patch.object(
                workers, "_latest_activity", return_value="正在加载上下文…"
            ), patch.object(
                workers.cards, "codex_starting_card", return_value={"starting": True}
            ) as starting_card, patch.object(
                workers.feishu, "update_card",
                side_effect=lambda *_: workers.final_path("oc_timer").write_text("完成", encoding="utf-8"),
            ), patch.object(workers, "deliver_with_fallback"):
                self.assertTrue(workers.begin_active_turn("oc_timer"))
                workers._ACTIVE_TURNS["oc_timer"]["started_at"] = 100.0
                workers.set_active_starting("oc_timer", True)

                workers.progress_worker("oc_timer", "message-id", workdir)

            starting_card.assert_called_once_with(12, "")

    def test_long_silence_keeps_existing_process_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            workdir = Path(tmp) / "bot"
            workdir.mkdir()
            clock = [100.0]
            updates = [0]

            def finish_after_second_update(*_args) -> None:
                updates[0] += 1
                if updates[0] == 1:
                    clock[0] = 500.0
                else:
                    workers.final_path("oc_silence").write_text("完成", encoding="utf-8")

            with patch.object(workers, "STATE", state), patch.object(
                workers.time, "time", side_effect=lambda: clock[0]
            ), patch.object(workers.time, "sleep"), patch.object(
                workers, "_active_size", return_value=0
            ), patch.object(
                workers, "_latest_activity", return_value="正在检查财务数据…"
            ), patch.object(
                workers.session, "codex_tui_ready", return_value=False
            ), patch.object(
                workers.cards,
                "thinking_card",
                side_effect=lambda activity, elapsed, supplement: {
                    "activity": activity,
                    "elapsed": elapsed,
                    "supplement": supplement,
                },
            ) as thinking_card, patch.object(
                workers.feishu, "update_card", side_effect=finish_after_second_update
            ), patch.object(workers, "deliver_with_fallback"):
                self.assertTrue(workers.begin_active_turn("oc_silence"))
                workers.progress_worker("oc_silence", "message-id", workdir)

            self.assertEqual(thinking_card.call_count, 2)
            self.assertEqual([call.args[0] for call in thinking_card.call_args_list], [
                "正在检查财务数据…",
                "正在检查财务数据…",
            ])
            self.assertEqual(thinking_card.call_args_list[-1].args[1], 400)


class ActiveTurnSupplementTests(unittest.TestCase):
    def tearDown(self) -> None:
        workers.end_active_turn("oc_test")

    def test_supplement_is_queued_while_codex_is_starting(self) -> None:
        self.assertTrue(workers.begin_active_turn("oc_test"))
        workers.set_active_starting("oc_test", True)

        active = workers.append_active_supplement(
            "oc_test",
            "[用户补充当前任务]\n请加上现金流风险",
        )

        self.assertIsNotNone(active)
        self.assertFalse(active["accepting"])
        self.assertEqual(active["supplement_note"], "用户进行了额外输入")
        self.assertEqual(
            workers.activate_turn("oc_test"),
            ["[用户补充当前任务]\n请加上现金流风险"],
        )

    def test_running_followup_freezes_old_card_and_opens_new_card(self) -> None:
        self.assertTrue(workers.begin_active_turn("oc_test"))
        workers.set_active_card("oc_test", "progress-card")
        workers.set_active_activity("oc_test", "正在分析财务数据…")
        workers.activate_turn("oc_test")

        with patch.object(server.session, "send") as send, patch.object(
            server.feishu, "update_card"
        ) as update_card, patch.object(
            server.feishu, "send_card", return_value={"data": {"message_id": "followup-card"}}
        ) as send_card:
            appended = server._append_to_active_turn(
                "oc_test",
                Path("/tmp/bot"),
                "请再补充现金流风险",
                [],
                apply_helper_hint=False,
            )

        self.assertTrue(appended)
        send.assert_called_once()
        send_card.assert_called_once()
        update_card.assert_called_once()
        old_rendered = json.dumps(update_card.call_args.args[1], ensure_ascii=False)
        new_rendered = json.dumps(send_card.call_args.args[1], ensure_ascii=False)
        self.assertIn("正在分析财务数据", old_rendered)
        self.assertIn("用户进行了额外输入", old_rendered)
        self.assertNotIn("请再补充现金流风险", old_rendered)
        self.assertIn('"tag": "note"', old_rendered)
        self.assertNotIn('"action": "stop"', old_rendered)
        self.assertIn("正在分析财务数据", new_rendered)
        self.assertIn('"action": "stop"', new_rendered)
        self.assertNotIn("用户进行了额外输入", new_rendered)
        current = workers.active_turn("oc_test")
        self.assertEqual(current["message_id"], "followup-card")
        self.assertEqual(current["supplement_note"], "")

    def test_final_reply_is_delivered_to_rotated_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            workdir = Path(tmp) / "bot"
            workdir.mkdir()
            with patch.object(workers, "STATE", state), patch.object(
                workers, "deliver_with_fallback"
            ) as deliver:
                self.assertTrue(workers.begin_active_turn("oc_test"))
                workers.set_active_card("oc_test", "old-card")
                workers.rotate_active_card("oc_test", "followup-card")
                workers.final_path("oc_test").write_text("追问后的完整回复", encoding="utf-8")

                workers.progress_worker("oc_test", "old-card", workdir)

            self.assertEqual(deliver.call_args.args[1], "followup-card")
            self.assertEqual(deliver.call_args.args[2], "追问后的完整回复")


class AttachmentPromptTests(unittest.TestCase):
    def test_attachments_are_injected_as_one_absolute_path_per_line(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "report one.pdf"
            second = Path(tmp) / "data.xlsx"
            first.write_bytes(b"pdf")
            second.write_bytes(b"xlsx")
            attachments = [
                server.Attachment("file", first, first.name),
                server.Attachment("file", second, second.name),
            ]

            prompt = server._reference_prompt("请提炼重点", attachments)

            self.assertEqual(
                prompt,
                f"【附件】\n{first.resolve()}\n{second.resolve()}\n\n请提炼重点",
            )
            self.assertNotIn("JSON", prompt)
            self.assertNotIn("attachments.json", prompt)


class HubGitTests(unittest.TestCase):
    def test_scoped_commit_does_not_include_unrelated_hub_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
            (repo / "resource.txt").write_text("v1\n", encoding="utf-8")
            (repo / "unrelated.txt").write_text("v1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
            subprocess.run([
                "git", "-C", str(repo), "-c", "user.email=test@example.com", "-c", "user.name=test",
                "commit", "-m", "initial",
            ], check=True, capture_output=True)
            (repo / "resource.txt").write_text("v2\n", encoding="utf-8")
            (repo / "unrelated.txt").write_text("v2\n", encoding="utf-8")

            with patch.object(hub, "HUB", repo):
                result = hub.git_commit("resource update", ["resource.txt"])

            status = subprocess.run(
                ["git", "-C", str(repo), "status", "--short"], check=True, capture_output=True, text=True
            ).stdout
            self.assertNotIn("resource.txt", status)
            self.assertIn("unrelated.txt", status)
            self.assertNotIn("nothing to commit", result.lower())


if __name__ == "__main__":
    unittest.main()
