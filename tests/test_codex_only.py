from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import server
from utils import chatconfig, feishu, panel, session


class CodexOnlyConfigTests(unittest.TestCase):
    def test_default_config_is_codex_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)

            cfg = chatconfig.get(workdir)

            self.assertEqual(cfg["engine"], "codex")
            self.assertEqual(cfg["codex_model"], chatconfig.DEFAULTS["codex_model"])
            self.assertEqual(cfg["codex_effort"], chatconfig.DEFAULTS["codex_effort"])

    def test_engine_is_always_persisted_as_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)

            cfg = chatconfig.set_value(workdir, "engine", "codex")
            stored = json.loads((workdir / "agent.json").read_text(encoding="utf-8"))

            self.assertEqual(cfg["engine"], "codex")
            self.assertEqual(stored["engine"], "codex")

    def test_legacy_reply_settings_are_ignored_and_cleaned_on_save(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            (workdir / "agent.json").write_text(
                json.dumps({"reply_mode": "all", "reply_format": "raw", "codex_model": "gpt-5"}),
                encoding="utf-8",
            )

            cfg = chatconfig.get(workdir)
            self.assertNotIn("reply_mode", cfg)
            self.assertNotIn("reply_format", cfg)

            chatconfig.set_value(workdir, "codex_effort", "medium")
            stored = json.loads((workdir / "agent.json").read_text(encoding="utf-8"))
            self.assertNotIn("reply_mode", stored)
            self.assertNotIn("reply_format", stored)


class CodexOnlySurfaceTests(unittest.TestCase):
    def test_control_panel_hides_help_and_format_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            menu = json.dumps(panel.main_menu_card(workdir, "oc_test"), ensure_ascii=False)
            settings = json.dumps(panel.settings_menu_card(workdir), ensure_ascii=False)
            reply = json.dumps(panel.reply_card(workdir), ensure_ascii=False)

            self.assertNotIn("帮助文档", menu)
            self.assertNotIn("help_doc", menu)
            self.assertNotIn("返回格式", settings)
            self.assertNotIn("page_format", settings)
            self.assertIn("自动判断", reply)
            self.assertNotIn("set_reply", reply)

    def test_welcome_card_explains_commands_and_local_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            chatconfig.init_defaults(workdir)

            rendered = json.dumps(panel.welcome_card(workdir, "oc_test"), ensure_ascii=False)

            self.assertIn("/status", rendered)
            self.assertIn("/privacy", rendered)
            self.assertIn("仅保存在运行机器", rendered)

    def test_empty_mcp_page_explains_private_local_setup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rendered = json.dumps(panel.mcp_card(Path(tmp)), ensure_ascii=False)

            self.assertIn("当前没有 MCP", rendered)
            self.assertIn("只会保存在本机当前会话", rendered)

    def test_group_reply_mode_is_based_on_human_member_count(self) -> None:
        with patch.object(feishu, "chat_human_member_count", return_value=1):
            self.assertEqual(server._automatic_group_reply_mode("oc_test"), "all")
        for count in (2, 20, 0, None):
            with self.subTest(count=count), patch.object(
                feishu, "chat_human_member_count", return_value=count
            ):
                self.assertEqual(server._automatic_group_reply_mode("oc_test"), "at_only")

    def test_human_member_count_is_cached_and_excludes_api_failures(self) -> None:
        feishu._chat_human_count_cache.clear()
        one_person = {
            "code": 0,
            "data": {"items": [{"member_id": "ou_human"}], "has_more": False},
        }
        with patch.object(feishu, "_token", return_value="token"), patch.object(
            feishu, "_request", return_value=one_person
        ) as request:
            self.assertEqual(feishu.chat_human_member_count("oc_single"), 1)
            self.assertEqual(feishu.chat_human_member_count("oc_single"), 1)
            request.assert_called_once()

        with patch.object(feishu, "_token", return_value="token"), patch.object(
            feishu, "_request", return_value={"code": 999, "msg": "denied"}
        ):
            self.assertIsNone(feishu.chat_human_member_count("oc_denied"))

    def test_feishu_client_uses_fast_reconnect_interval(self) -> None:
        configured = dict(server.CONFIG)
        configured["feishu"] = {"app_id": "cli_test_only", "app_secret": "test-only-secret"}
        with patch.object(server, "CONFIG", configured):
            client = server.build_client()

        self.assertEqual(client._reconnect_interval, 10)
        self.assertEqual(client._reconnect_nonce, 3)

    def test_feishu_websocket_host_bypasses_http_proxy(self) -> None:
        with patch.dict(os.environ, {"NO_PROXY": "localhost", "no_proxy": ""}, clear=False):
            server._ensure_feishu_ws_no_proxy()

            self.assertIn("msg-frontier.feishu.cn", os.environ["NO_PROXY"].split(","))
            self.assertIn("msg-frontier.feishu.cn", os.environ["no_proxy"].split(","))

    def test_model_card_contains_only_codex_choices(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            rendered = json.dumps(panel.model_card(Path(tmp)), ensure_ascii=False)

            self.assertIn("Codex", rendered)
            self.assertIn("GPT-5.6 Sol", rendered)
            self.assertIn("GPT-5.6 Terra", rendered)
            self.assertIn("GPT-5.6 Luna", rendered)
            self.assertIn("stage_codex_model", rendered)
            self.assertIn("stage_codex_effort", rendered)
            self.assertEqual(rendered.count("apply_codex_settings"), 1)

    def test_launch_command_is_always_codex(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            (workdir / "agent.json").write_text(
                json.dumps({"engine": "codex", "codex_model": "gpt-5"}),
                encoding="utf-8",
            )

            command = session.build_command(workdir)

            self.assertTrue(command.startswith("codex --model gpt-5 "))
            self.assertIn("model_reasoning_effort=high", command)
            self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
            self.assertIn("--dangerously-bypass-hook-trust", command)

    def test_tmux_session_names_use_feishu_prefix(self) -> None:
        self.assertEqual(session.session_name("oc_123"), "feishu-oc_123")
        self.assertEqual(session.session_name("oc:unsafe"), "feishu-oc_unsafe")

    def test_cold_start_notifies_before_spawning_codex(self) -> None:
        events = []

        with patch.object(session, "exists", return_value=False), patch.object(
            session, "latest_codex_session", return_value=None
        ), patch.object(session, "_spawn", side_effect=lambda *_: events.append("spawn")), patch.object(
            session, "_wait_codex_ready", return_value=True
        ):
            cold = session.ensure(
                "oc_123",
                Path("/tmp/bot"),
                cold_wait=0,
                on_start=lambda: events.append("notify"),
            )

        self.assertTrue(cold)
        self.assertEqual(events, ["notify", "spawn"])

    def test_warm_session_does_not_show_starting_state(self) -> None:
        notified = []
        with patch.object(session, "exists", return_value=True), patch.object(
            session, "_agent_alive", return_value=True
        ):
            cold = session.ensure(
                "oc_123",
                Path("/tmp/bot"),
                cold_wait=0,
                on_start=lambda: notified.append(True),
            )

        self.assertFalse(cold)
        self.assertEqual(notified, [])

    def test_tui_ready_requires_process_header_and_prompt(self) -> None:
        ready_pane = "│ >_ OpenAI Codex (v0.144.5) │\n› Summarize recent commits\n"

        def fake_tmux(*args: str):
            if args[0] == "capture-pane":
                return SimpleNamespace(stdout=ready_pane)
            return SimpleNamespace(stdout="0\tcodex-aarch64-apple-darwin\n")

        with patch.object(session, "_tmux", side_effect=fake_tmux):
            self.assertTrue(session._codex_tui_ready("oc_123"))

        with patch.object(session, "_codex_pane_text", return_value=">_ OpenAI Codex (v0.144.5)"), patch.object(
            session, "_codex_process_alive", return_value=True
        ):
            self.assertFalse(session._codex_tui_ready("oc_123"))

    def test_mcp_degraded_startup_is_reported_separately(self) -> None:
        pane = "⚠ MCP startup incomplete (failed: example-a, example-b)\n› Ready"
        with patch.object(session, "_codex_pane_text", return_value=pane):
            self.assertEqual(session.codex_startup_warning("oc_123"), "example-a, example-b")

    def test_wait_ready_does_not_require_new_rollout_file(self) -> None:
        pane = ">_ OpenAI Codex (v0.144.6)\n› Ask anything\n"
        with patch.object(session, "_CODEX_READY_SETTLE_SEC", 0.01), patch.object(
            session, "_codex_pane_text", return_value=pane
        ) as capture, patch.object(
            session, "_codex_process_alive", return_value=True
        ), patch.object(session, "latest_codex_session", return_value=None):
            ready = session._wait_codex_ready(
                "oc_123", Path("/tmp/bot"), before=None, timeout=0.5, after=time.time()
            )

        self.assertTrue(ready)
        self.assertGreaterEqual(capture.call_count, 2)

    def test_recent_session_lookup_can_exclude_previous_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            workdir = Path(tmp) / "bot"
            day = time.strftime("%Y/%m/%d")
            folder = root / day
            folder.mkdir(parents=True)
            payload = json.dumps({"type": "session_meta", "payload": {"cwd": str(workdir.resolve())}}) + "\n"
            old = folder / "rollout-old.jsonl"
            new = folder / "rollout-new.jsonl"
            old.write_text(payload, encoding="utf-8")
            new.write_text(payload, encoding="utf-8")
            now = time.time()
            os.utime(old, (now - 10, now - 10))
            os.utime(new, (now, now))

            with patch.object(session, "_CODEX_SESSIONS_DIR", root):
                session._CODEX_SESSION_CACHE.clear()
                found = session.latest_codex_session(workdir, after=now - 2, exclude=old)

            self.assertEqual(found, new)

    def test_effort_choices_follow_selected_model(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            sol = json.dumps(
                panel.model_card(workdir, {"model": "gpt-5.6-sol", "effort": "ultra"}),
                ensure_ascii=False,
            )
            luna = json.dumps(
                panel.model_card(workdir, {"model": "gpt-5.6-luna", "effort": "ultra"}),
                ensure_ascii=False,
            )

            self.assertIn("Ultra", sol)
            self.assertNotIn("Ultra", luna)
            self.assertIn("Medium", luna)

    def test_apply_model_and_effort_starts_only_one_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp)
            with patch.object(server.session, "exists", return_value=True), patch.object(
                server.threading, "Thread"
            ) as thread:
                message = server._apply_codex_settings("oc_123", workdir, "gpt-5.6-sol", "xhigh")

            cfg = chatconfig.get(workdir)
            self.assertEqual(cfg["codex_model"], "gpt-5.6-sol")
            self.assertEqual(cfg["codex_effort"], "xhigh")
            self.assertEqual(thread.call_count, 1)
            thread.return_value.start.assert_called_once_with()
            self.assertIn("仅重启一次", message)


if __name__ == "__main__":
    unittest.main()
