from __future__ import annotations

import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from utils import config


class ConfigurationSecurityTests(unittest.TestCase):
    def test_dotenv_does_not_override_process_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("SAFE_TEST_KEY=from-file\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"SAFE_TEST_KEY": "from-process"}, clear=False):
                config.load_dotenv(path)
                self.assertEqual(os.environ["SAFE_TEST_KEY"], "from-process")

    def test_config_expands_env_and_missing_values_become_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"a":"${SAFE_TEST_VALUE}","b":"${MISSING_TEST_VALUE}"}', encoding="utf-8")
            with mock.patch.dict(os.environ, {"SAFE_TEST_VALUE": "present"}, clear=False):
                loaded = config.load(path)
            self.assertEqual(loaded, {"a": "present", "b": ""})

    def test_mcp_expansion_preserves_bundle_placeholder(self):
        with mock.patch.dict(os.environ, {"SAFE_TEST_VALUE": "present"}, clear=False):
            loaded = config.expand_env(["${SAFE_TEST_VALUE}", "${MCP_BUNDLE_DIR}/server.py"])
        self.assertEqual(loaded, ["present", "${MCP_BUNDLE_DIR}/server.py"])

    def test_redaction_covers_urls_bearer_headers_and_known_values(self):
        with mock.patch.dict(os.environ, {"FEISHU_APP_SECRET": "very-secret-value"}, clear=False):
            text = config.redact_text(
                "wss://example/ws?access_key=abc123&ticket=def456 "
                "Authorization: Bearer token.value very-secret-value"
            )
        self.assertNotIn("abc123", text)
        self.assertNotIn("def456", text)
        self.assertNotIn("token.value", text)
        self.assertNotIn("very-secret-value", text)
        self.assertGreaterEqual(text.count("[REDACTED]"), 4)

    def test_logging_factory_redacts_third_party_messages(self):
        config.install_log_redaction()
        record = logging.getLogger("security-test").makeRecord(
            "security-test", logging.INFO, __file__, 1,
            "connected?access_key=plain-secret", (), None,
        )
        self.assertNotIn("plain-secret", record.getMessage())


if __name__ == "__main__":
    unittest.main()
