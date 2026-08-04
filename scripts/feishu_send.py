#!/usr/bin/env python3
"""Send Feishu messages from chat-agent workdirs.

This is the shared implementation used by the feishu-send skill. It can be
called from a bot workdir as long as CHAT_AGENT_HOME points to chat-agent root.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


def _repo_root() -> Path:
    env = os.environ.get("CHAT_AGENT_HOME")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


ROOT = _repo_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from utils import cards, feishu, html_card  # noqa: E402


def _safe_chat(chat_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", chat_id)


def _chat_id(args: argparse.Namespace) -> str:
    value = args.chat_id or os.environ.get("CHAT_ID", "")
    if not value:
        raise SystemExit("CHAT_ID is missing. Pass --chat-id or run inside chat-agent.")
    return value


def _mark_sent(chat_id: str, summary: str) -> None:
    # 独立定时任务不能污染交互对话的 .sent 终稿状态。
    if os.environ.get("CHAT_AGENT_NO_SENT_MARKER") == "1":
        return
    state = ROOT / "state"
    state.mkdir(exist_ok=True)
    (state / f"{_safe_chat(chat_id)}.sent").write_text(summary, encoding="utf-8")


def _print_result(result: dict[str, Any]) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _read_text(path: str) -> str:
    p = Path(path).expanduser()
    if not p.is_file():
        raise SystemExit(f"file not found: {p}")
    return p.read_text(encoding="utf-8")


def _read_json(path: str) -> Any:
    return json.loads(_read_text(path))


def _check_path(path: str, label: str) -> Path:
    p = Path(path).expanduser().resolve()
    if not p.is_file():
        raise SystemExit(f"{label} not found: {p}")
    return p


def _done(args: argparse.Namespace, chat_id: str, summary: str, result: dict[str, Any]) -> None:
    if args.dry_run:
        result["dry_run"] = True
    else:
        _mark_sent(chat_id, summary)
    _print_result(result)


def do_text(args: argparse.Namespace) -> None:
    chat_id = _chat_id(args)
    if not args.dry_run:
        res = feishu.send_text(chat_id, args.text)
        message_id = (res.get("data") or {}).get("message_id", "")
    else:
        message_id = ""
    _done(args, chat_id, "已发送文本。", {"sent": "text", "message_id": message_id})


def do_file(args: argparse.Namespace) -> None:
    chat_id = _chat_id(args)
    path = _check_path(args.path, "file")
    if not args.dry_run:
        file_key = feishu.upload_file(path, args.file_type)
        res = feishu.send_file(chat_id, file_key)
        message_id = (res.get("data") or {}).get("message_id", "")
    else:
        file_key = ""
        message_id = ""
    _done(
        args,
        chat_id,
        f"已发送文件：{path.name}",
        {"sent": "file", "path": str(path), "file_type": args.file_type, "file_key": file_key, "message_id": message_id},
    )


def do_image(args: argparse.Namespace) -> None:
    chat_id = _chat_id(args)
    path = _check_path(args.path, "image")
    if not args.dry_run:
        image_key = feishu.upload_image(path)
        res = feishu.send_image(chat_id, image_key)
        message_id = (res.get("data") or {}).get("message_id", "")
    else:
        image_key = ""
        message_id = ""
    _done(
        args,
        chat_id,
        f"已发送图片：{path.name}",
        {"sent": "image", "path": str(path), "image_key": image_key, "message_id": message_id},
    )


def _send_card(args: argparse.Namespace, card: dict, summary: str, kind: str) -> None:
    chat_id = _chat_id(args)
    if not args.dry_run:
        res = feishu.send_card(chat_id, card)
        message_id = (res.get("data") or {}).get("message_id", "")
    else:
        message_id = ""
    _done(args, chat_id, summary, {"sent": kind, "message_id": message_id})


def do_card(args: argparse.Namespace) -> None:
    card = json.loads(args.json)
    if not isinstance(card, dict):
        raise SystemExit("card JSON must be an object.")
    _send_card(args, card, "已发送卡片。", "card")


def do_card_file(args: argparse.Namespace) -> None:
    card = _read_json(args.path)
    if not isinstance(card, dict):
        raise SystemExit("card JSON file must contain an object.")
    _send_card(args, card, f"已发送卡片：{Path(args.path).name}", "card-file")


def do_md(args: argparse.Namespace) -> None:
    _send_card(args, cards.markdown_card(args.text), "已发送 Markdown 卡片。", "md")


def do_md_file(args: argparse.Namespace) -> None:
    path = _check_path(args.path, "markdown file")
    text = path.read_text(encoding="utf-8")
    _send_card(args, cards.markdown_card(text), f"已发送 Markdown 卡片：{path.name}", "md-file")


def do_html(args: argparse.Namespace) -> None:
    text = html_card.to_markdown(args.html)
    _send_card(args, cards.markdown_card(text), "已发送 HTML 渲染卡片。", "html")


def do_html_file(args: argparse.Namespace) -> None:
    path = _check_path(args.path, "html file")
    text = html_card.to_markdown(path.read_text(encoding="utf-8", errors="ignore"))
    _send_card(args, cards.markdown_card(text), f"已发送 HTML 渲染卡片：{path.name}", "html-file")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Send Feishu messages for chat-agent.")
    p.add_argument("--chat-id", default="", help="Feishu chat_id. Defaults to CHAT_ID.")
    p.add_argument("--dry-run", action="store_true", help="Validate input and print the action without sending.")
    sub = p.add_subparsers(dest="cmd", required=True)

    text = sub.add_parser("text", help="Send a plain text message.")
    text.add_argument("text")
    text.set_defaults(fn=do_text)

    file = sub.add_parser("file", help="Upload and send a file.")
    file.add_argument("path")
    file.add_argument("--file-type", default="stream", choices=["stream", "doc", "xls", "ppt", "pdf", "image", "video", "audio"])
    file.set_defaults(fn=do_file)

    image = sub.add_parser("image", help="Upload and send an image message.")
    image.add_argument("path")
    image.set_defaults(fn=do_image)

    card = sub.add_parser("card", help="Send a Feishu interactive card from JSON text.")
    card.add_argument("json")
    card.set_defaults(fn=do_card)

    card_file = sub.add_parser("card-file", help="Send a Feishu interactive card from a JSON file.")
    card_file.add_argument("path")
    card_file.set_defaults(fn=do_card_file)

    md = sub.add_parser("md", help="Render Markdown-like text as a Feishu card and send it.")
    md.add_argument("text")
    md.set_defaults(fn=do_md)

    md_file = sub.add_parser("md-file", help="Render a Markdown file as a Feishu card and send it.")
    md_file.add_argument("path")
    md_file.set_defaults(fn=do_md_file)

    html = sub.add_parser("html", help="Convert HTML text to a Feishu-rendered card.")
    html.add_argument("html")
    html.set_defaults(fn=do_html)

    html_file = sub.add_parser("html-file", help="Convert an HTML file to a Feishu-rendered card.")
    html_file.add_argument("path")
    html_file.set_defaults(fn=do_html_file)

    return p


def main() -> None:
    args = build_parser().parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
