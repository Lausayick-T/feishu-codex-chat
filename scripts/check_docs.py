#!/usr/bin/env python3
"""Validate local Markdown links without making network requests."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parent.parent
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
SCHEMES = ("http://", "https://", "mailto:", "tel:")


def markdown_files() -> list[Path]:
    excluded = {".git", ".venv", "state", "bots"}
    return sorted(
        path
        for path in ROOT.rglob("*.md")
        if not any(part in excluded for part in path.relative_to(ROOT).parts)
    )


def main() -> int:
    failures: list[str] = []
    checked = 0

    for source in markdown_files():
        text = source.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), 1):
            for raw_target in LINK.findall(line):
                target = raw_target.strip()
                if target.startswith("<") and target.endswith(">"):
                    target = target[1:-1]
                target = target.split(maxsplit=1)[0]
                if not target or target.startswith("#") or target.startswith(SCHEMES):
                    continue

                path_text = unquote(target.split("#", 1)[0].split("?", 1)[0])
                destination = (source.parent / path_text).resolve()
                checked += 1
                try:
                    destination.relative_to(ROOT)
                except ValueError:
                    failures.append(
                        f"{source.relative_to(ROOT)}:{line_number}: 链接越出仓库：{target}"
                    )
                    continue
                if not destination.exists():
                    failures.append(
                        f"{source.relative_to(ROOT)}:{line_number}: 目标不存在：{target}"
                    )

    if failures:
        print(f"文档检查失败：{len(failures)} 项")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print(f"文档检查通过：扫描 {len(markdown_files())} 个 Markdown 文件、{checked} 个本地链接。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
