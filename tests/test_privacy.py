from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from utils import chatconfig, privacy, scheduled


class PrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workdir = self.root / "bot"
        self.workdir.mkdir()
        self.db = self.root / "scheduler.db"
        self.db_patch = patch.object(scheduled, "SCHEDULER_DB", self.db)
        self.db_patch.start()
        chatconfig.init_defaults(self.workdir)

    def tearDown(self) -> None:
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_inventory_and_manual_cleanup_are_scoped(self) -> None:
        attachment = self.workdir / "incoming" / "message" / "file.txt"
        attachment.parent.mkdir(parents=True)
        attachment.write_text("private attachment", encoding="utf-8")
        outside = self.root / "outside.txt"
        outside.write_text("keep", encoding="utf-8")

        before = privacy.inventory(self.workdir)
        removed = privacy.clear_category(self.workdir, "attachments")

        self.assertEqual(before["attachments"]["files"], 1)
        self.assertEqual(removed, 1)
        self.assertFalse(attachment.exists())
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")

    def test_retention_removes_only_expired_attachments(self) -> None:
        old = self.workdir / "incoming" / "old.txt"
        new = self.workdir / "incoming" / "new.txt"
        old.parent.mkdir(parents=True)
        old.write_text("old", encoding="utf-8")
        new.write_text("new", encoding="utf-8")
        now = time.time()
        os.utime(old, (now - 40 * 86400, now - 40 * 86400))
        chatconfig.set_value(self.workdir, "attachment_retention_days", 30)

        result = privacy.cleanup_expired(self.workdir, now=now)

        self.assertEqual(result["attachments"], 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_clear_memory_recreates_blank_summary_files(self) -> None:
        memory = self.workdir / "memory" / "MEMORY.md"
        memory.write_text("private memory", encoding="utf-8")

        privacy.clear_category(self.workdir, "memory")

        self.assertTrue(memory.exists())
        self.assertNotIn("private memory", memory.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
