from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CONTROLLER = ROOT / "scripts" / "servicectl.sh"


class ServiceControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.state = self.root / "state"
        (self.root / "server.py").write_text(
            "import time\nprint('server-ready', flush=True)\ntime.sleep(30)\n",
            encoding="utf-8",
        )
        (self.root / "scheduler_service.py").write_text(
            "import time\nprint('scheduler-ready', flush=True)\ntime.sleep(30)\n",
            encoding="utf-8",
        )
        self.session = f"chat-agent-test-{uuid.uuid4().hex[:10]}"
        self.env = {
            **os.environ,
            "CHAT_AGENT_ROOT": str(self.root),
            "CHAT_AGENT_PYTHON": sys.executable,
            "CHAT_AGENT_STATE_DIR": str(self.state),
            "CHAT_AGENT_TMUX_SESSION": self.session,
            "CHAT_AGENT_PLATFORM": "macos",
            "CHAT_AGENT_AUTOSTART_BOOT": "1",
            "CHAT_AGENT_IGNORE_LEGACY_TMUX": "1",
        }

    def tearDown(self) -> None:
        if shutil.which("tmux"):
            subprocess.run(
                ["tmux", "kill-session", "-t", f"={self.session}"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        self._tmp.cleanup()

    def run_ctl(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(CONTROLLER), *args],
            text=True,
            errors="replace",
            capture_output=True,
            env=self.env,
            timeout=15,
            check=check,
        )

    def test_default_foreground_stops_peer_when_one_service_exits(self) -> None:
        (self.root / "server.py").write_text(
            "import time\nprint('server-ready', flush=True)\ntime.sleep(0.2)\n",
            encoding="utf-8",
        )

        completed = self.run_ctl(check=False)

        self.assertEqual(
            completed.returncode,
            1,
            msg=f"stdout={completed.stdout!r}\nstderr={completed.stderr!r}",
        )
        self.assertIn("server 已在前台托管", completed.stdout)
        self.assertIn("scheduler 已在前台托管", completed.stdout)
        self.assertIn("正在停止另一个服务", completed.stdout)

    @unittest.skipUnless(shutil.which("tmux"), "tmux is not installed")
    def test_tmux_uses_one_session_with_two_named_windows(self) -> None:
        self.run_ctl("tmux", "start")
        windows = subprocess.run(
            ["tmux", "list-windows", "-t", f"={self.session}", "-F", "#{window_name}"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.splitlines()

        self.assertEqual(windows, ["server", "scheduler"])
        self.run_ctl("tmux", "stop")

    def test_nohup_tracks_both_processes(self) -> None:
        self.run_ctl("nohup", "start")
        status = self.run_ctl("nohup", "status")
        self.assertIn("server：运行中", status.stdout)
        self.assertIn("scheduler：运行中", status.stdout)
        self.run_ctl("nohup", "stop")

    def test_autostart_renders_one_tmux_launcher_per_platform(self) -> None:
        macos = self.root / "rendered-macos"
        linux = self.root / "rendered-linux"
        self.run_ctl("autostart", "render", "macos", str(macos))
        self.run_ctl("autostart", "render", "linux", str(linux))

        plist = (macos / "io.github.feishu-codex-chat.plist").read_text(encoding="utf-8")
        unit = (linux / "feishu-codex-chat.service").read_text(encoding="utf-8")
        self.assertIn("<string>tmux</string>", plist)
        self.assertIn("<string>start</string>", plist)
        self.assertNotIn("<key>KeepAlive</key>", plist)
        self.assertIn("Type=oneshot", unit)
        self.assertIn(" tmux start", unit)
        self.assertIn(" tmux stop", unit)
        self.assertEqual(len(list(macos.iterdir())), 1)
        self.assertEqual(len(list(linux.iterdir())), 1)

    def test_autostart_dry_run_handles_empty_legacy_lists(self) -> None:
        completed = self.run_ctl("--dry-run", "autostart", "install")

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("LaunchAgent", completed.stdout)

    def test_doctor_reports_ready_offline_environment(self) -> None:
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        for name in ("uv", "tmux", "codex"):
            executable = bin_dir / name
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o755)
        (self.root / "bots" / "_template").mkdir(parents=True)
        (self.root / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
        (self.root / "config.json").write_text("{}\n", encoding="utf-8")
        env_file = self.root / ".env"
        env_file.write_text("FEISHU_APP_ID=test-id\nFEISHU_APP_SECRET=test-secret\n", encoding="utf-8")
        env_file.chmod(0o600)
        self.env["PATH"] = f"{bin_dir}:{self.env.get('PATH', '')}"

        completed = self.run_ctl("doctor", "--offline")

        self.assertIn("0 项失败", completed.stdout)
        self.assertNotIn("test-secret", completed.stdout)


if __name__ == "__main__":
    unittest.main()
