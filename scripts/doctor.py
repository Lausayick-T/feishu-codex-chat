#!/usr/bin/env python3
"""Preflight diagnostics for Feishu Codex Chat without exposing credentials."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


FEISHU_BASE = "https://open.feishu.cn/open-apis"


@dataclass
class Result:
    level: str
    name: str
    detail: str


def _dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            values[key] = value
    return values


def _request(url: str, *, method: str = "GET", payload: dict | None = None,
             token: str = "") -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=8) as response:
        value = json.loads(response.read().decode("utf-8"))
    return value if isinstance(value, dict) else {}


def run(root: Path, *, offline: bool = False) -> list[Result]:
    results: list[Result] = []

    version = sys.version_info
    results.append(Result(
        "ok" if version >= (3, 10) else "fail",
        "Python",
        f"{version.major}.{version.minor}.{version.micro}",
    ))

    for command in ("uv", "tmux", "codex"):
        path = shutil.which(command)
        results.append(Result("ok" if path else "fail", command, path or "未安装或不在 PATH"))

    if shutil.which("codex"):
        try:
            completed = subprocess.run(
                ["codex", "login", "status"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                check=False,
            )
            results.append(Result(
                "ok" if completed.returncode == 0 else "fail",
                "Codex 登录",
                "已登录" if completed.returncode == 0 else "未登录；请先执行 codex login",
            ))
        except (OSError, subprocess.TimeoutExpired):
            results.append(Result("fail", "Codex 登录", "状态检查失败"))

    for relative in ("pyproject.toml", "config.json", "bots/_template"):
        path = root / relative
        results.append(Result("ok" if path.exists() else "fail", relative, "存在" if path.exists() else "缺失"))

    try:
        config_text = (root / "config.json").read_text(encoding="utf-8")
        config_document = json.loads(config_text)
        if not isinstance(config_document, dict):
            raise ValueError("配置根节点不是对象")
    except (OSError, json.JSONDecodeError, ValueError):
        config_text = ""
        if (root / "config.json").exists():
            results.append(Result("fail", "config.json 格式", "不是有效的 JSON 对象"))
    else:
        results.append(Result("ok", "config.json 格式", "有效"))
    if "dangerously-bypass-approvals-and-sandbox" in config_text:
        results.append(Result(
            "warn",
            "Codex 权限",
            "启用了无人值守高权限模式；请使用专用低权限系统账户",
        ))

    env_path = root / ".env"
    env = {**_dotenv(env_path), **os.environ}
    if not env_path.exists():
        process_configured = bool(os.environ.get("FEISHU_APP_ID") and os.environ.get("FEISHU_APP_SECRET"))
        results.append(Result(
            "warn" if process_configured else "fail",
            ".env",
            "未使用；已从进程环境读取凭证" if process_configured else "不存在；请复制 .env.example 并填写",
        ))
    else:
        mode = stat.S_IMODE(env_path.stat().st_mode)
        level = "ok" if mode & 0o077 == 0 else "warn"
        detail = f"权限 {mode:04o}" + ("" if level == "ok" else "；建议执行 chmod 600 .env")
        results.append(Result(level, ".env", detail))

    app_id = str(env.get("FEISHU_APP_ID") or "").strip()
    app_secret = str(env.get("FEISHU_APP_SECRET") or "").strip()
    results.append(Result("ok" if app_id else "fail", "飞书 App ID", "已配置" if app_id else "未配置"))
    results.append(Result("ok" if app_secret else "fail", "飞书 App Secret", "已配置" if app_secret else "未配置"))

    if offline:
        results.append(Result("warn", "飞书在线验证", "已通过 --offline 跳过"))
    elif app_id and app_secret:
        try:
            auth = _request(
                f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal",
                method="POST",
                payload={"app_id": app_id, "app_secret": app_secret},
            )
            token = str(auth.get("tenant_access_token") or "")
            if auth.get("code") != 0 or not token:
                results.append(Result("fail", "飞书凭证", f"验证失败（code={auth.get('code', 'unknown')}）"))
            else:
                results.append(Result("ok", "飞书凭证", "有效"))
                bot = _request(f"{FEISHU_BASE}/bot/v3/info", token=token)
                results.append(Result(
                    "ok" if bot.get("code") == 0 else "warn",
                    "机器人能力",
                    "可用" if bot.get("code") == 0 else f"未确认（code={bot.get('code', 'unknown')}）",
                ))
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            results.append(Result("fail", "飞书在线验证", "网络请求失败；可用 --offline 仅检查本机配置"))

    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="检查 Feishu Codex Chat 启动条件")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--offline", action="store_true", help="跳过飞书在线凭证验证")
    args = parser.parse_args()

    results = run(args.root.resolve(), offline=args.offline)
    icons = {"ok": "✅", "warn": "⚠️", "fail": "❌"}
    print("Feishu Codex Chat 启动诊断\n")
    for result in results:
        print(f"{icons[result.level]} {result.name}：{result.detail}")
    failures = sum(item.level == "fail" for item in results)
    warnings = sum(item.level == "warn" for item in results)
    print(f"\n结果：{failures} 项失败，{warnings} 项提醒。")
    if failures:
        print("修复失败项后重新运行 ./start.sh doctor。")
    else:
        print("本机启动条件已满足。")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
