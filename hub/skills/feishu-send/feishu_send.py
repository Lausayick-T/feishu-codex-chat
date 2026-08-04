#!/usr/bin/env python3
"""Compatibility wrapper for the project-level Feishu sender."""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


def _find_script() -> Path:
    env = os.environ.get("CHAT_AGENT_HOME")
    candidates = []
    if env:
        candidates.append(Path(env).expanduser().resolve() / "scripts" / "feishu_send.py")
    cur = Path.cwd().resolve()
    candidates.extend(parent / "scripts" / "feishu_send.py" for parent in [cur, *cur.parents])
    here = Path(__file__).resolve()
    candidates.extend(parent / "scripts" / "feishu_send.py" for parent in here.parents)
    for path in candidates:
        if path.is_file():
            return path
    raise SystemExit("Cannot find chat-agent/scripts/feishu_send.py. Set CHAT_AGENT_HOME to chat-agent root.")


if __name__ == "__main__":
    script = _find_script()
    sys.argv[0] = str(script)
    runpy.run_path(str(script), run_name="__main__")
