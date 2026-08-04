#!/usr/bin/env python3
"""Audit tracked files and reachable Git history without echoing matched values."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BUILTIN_SKILLS = {
    "chat-agent-maintenance",
    "feishu-send",
    "hub-publish",
    "scheduled-task",
}
SECRET_PATTERNS = {
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "AWS access key": re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "GitHub token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "OpenAI key": re.compile(rb"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    "JWT": re.compile(rb"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    "URL credentials": re.compile(rb"https?://[^\s/:@]+:[^\s/@]+@"),
    "real Feishu identifier": re.compile(rb"\b(?:cli|oc|ou|on)_[A-Za-z0-9]{12,}\b"),
    "personal home path": re.compile(rb"/(?:Users|home)/([^/\s\"']+)"),
    "email address": re.compile(rb"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
}
SENSITIVE_NAME = re.compile(
    r"(?i)(secret|token|password|passwd|api[_-]?key|authorization|credential|private[_-]?key)"
)
ASSIGNMENT = re.compile(
    r"^(?:export[ \t]+)?([A-Z][A-Z0-9_]*)[ \t]*=[ \t]*[\"']?([^\"'#, \t]*)"
)
PLACEHOLDER = re.compile(
    r"^(?:|\$\{[^}]+\}|<[^>]+>|你的.*|your(?:[-_].*)?|example(?:[-_].*)?|"
    r"dummy(?:[-_].*)?|test(?:[-_].*)?|changeme|none|null|x+|\*+)$",
    re.I,
)


def _git(*args: str, binary: bool = False):
    result = subprocess.run(
        ["git", "-C", str(ROOT), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout if binary else result.stdout.decode("utf-8", "replace")


def _blocked_path(path: str) -> str:
    parts = Path(path).parts
    name = Path(path).name.lower()
    if path == ".env" or name in {"registry.json", ".mcp.json"}:
        return "local configuration"
    if name.endswith((".jsonl", ".log", ".db", ".sqlite", ".sqlite3", ".pem", ".key")):
        return "runtime or credential file"
    if parts and parts[0] in {"state", ".catalysthub", ".codex", ".claude"}:
        return "local state"
    if len(parts) >= 2 and parts[0] == "bots" and parts[1] != "_template":
        return "conversation workspace"
    if len(parts) >= 3 and parts[:2] == ("hub", "mcp") and name.endswith(".json"):
        return "personal MCP configuration"
    if len(parts) >= 3 and parts[:2] == ("hub", "skills") and parts[2] not in BUILTIN_SKILLS:
        return "non-built-in Skill"
    return ""


def _dotenv_values() -> dict[str, bytes]:
    path = ROOT / ".env"
    if not path.exists():
        return {}
    values = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip("\"'")
        if len(value) >= 6:
            values[key.strip()] = value.encode()
    return values


def main() -> int:
    parser = argparse.ArgumentParser(description="审计公开 Git 树和可达历史")
    parser.add_argument("--current-only", action="store_true", help="只扫描当前提交")
    args = parser.parse_args()

    commits = ["HEAD"] if args.current_only else _git("rev-list", "--all").splitlines()
    if not commits:
        commits = ["HEAD"]
    findings: set[tuple[str, str, str, int]] = set()
    local_values = _dotenv_values()

    worktree_paths = _git(
        "ls-files", "--cached", "--others", "--exclude-standard"
    ).splitlines()
    targets: list[tuple[str, list[str]]] = [("WORKTREE", worktree_paths)]
    targets.extend(
        (commit, _git("ls-tree", "-r", "--name-only", commit).splitlines())
        for commit in commits
    )

    for commit, paths in targets:
        for path in paths:
            blocked = _blocked_path(path)
            if blocked:
                findings.add((blocked, commit[:7], path, 0))
            try:
                data = (
                    (ROOT / path).read_bytes()
                    if commit == "WORKTREE"
                    else _git("show", f"{commit}:{path}", binary=True)
                )
            except (OSError, subprocess.CalledProcessError):
                continue
            if b"\0" in data:
                findings.add(("binary file", commit[:7], path, 0))
                continue
            if path != "scripts/audit_public.py":
                for category, pattern in SECRET_PATTERNS.items():
                    for match in pattern.finditer(data):
                        matched = match.group(0).lower()
                        if category == "personal home path" and match.group(1).lower() == b"example":
                            continue
                        if category == "email address" and matched.endswith(
                            (b"@example.com", b"@example.org", b"@example.net")
                        ):
                            continue
                        findings.add((category, commit[:7], path, data.count(b"\n", 0, match.start()) + 1))
                text = data.decode("utf-8", "replace")
                for line_number, line in enumerate(text.splitlines(), 1):
                    match = ASSIGNMENT.match(line.strip())
                    if not match or not SENSITIVE_NAME.search(match.group(1)):
                        continue
                    documented_placeholder = re.search(r"(?i)\b(?:your|example|dummy|test)[-_ ]", line)
                    if (
                        not PLACEHOLDER.match(match.group(2))
                        and not match.group(2).startswith("${")
                        and not documented_placeholder
                    ):
                        findings.add(("literal sensitive assignment", commit[:7], path, line_number))
            for key, value in local_values.items():
                if value in data:
                    findings.add((f"matches local secret variable {key}", commit[:7], path, 0))

    if findings:
        print(f"公开发布审计失败：{len(findings)} 项")
        for category, commit, path, line in sorted(findings):
            location = f"{path}:{line}" if line else path
            print(f"- {category}: {commit} {location}")
        return 1

    print(
        f"公开发布审计通过：扫描当前工作树和 {len(commits)} 个提交；"
        f"比对本机敏感值 {len(local_values)} 项；未发现会话数据、个人资源或凭证。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
