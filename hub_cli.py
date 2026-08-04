#!/usr/bin/env python3
"""Conversational lifecycle CLI for dedicated and shared Codex Skills/MCPs."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils import hub  # noqa: E402


def _workdir(args: argparse.Namespace) -> Path:
    return Path(getattr(args, "src", os.getcwd())).expanduser().resolve()


def _print_report(report) -> None:
    print(report.format())


def _finish_publish(report, *, commit: bool, label: str, paths: list[str]) -> None:
    _print_report(report)
    if not report.ok:
        raise SystemExit(2)
    try:
        from utils import bitable

        if bitable.enabled():
            bitable.ensure_catalog()
            print("📊 已同步到多维表格目录表")
    except Exception as exc:
        print(f"（目录表刷新跳过：{exc}）")
    if commit:
        print(hub.git_commit(f"hub: publish {label}", paths))


def _add_workdir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--from", dest="src", default=os.getcwd(), help="会话工作目录（默认 cwd）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="通过 Codex 对话创建、验证和发布专用 Skill/MCP")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出系统、通用和当前群专用资源")

    skill_init = sub.add_parser("init-skill", help="在当前群创建专用 Skill 脚手架，不发布")
    skill_init.add_argument("name")
    skill_init.add_argument("--description", required=True)
    _add_workdir(skill_init)

    skill_validate = sub.add_parser("validate-skill", help="验证当前群专用 Skill")
    skill_validate.add_argument("name")
    skill_validate.add_argument("--publish", action="store_true", help="按通用化发布标准检查")
    _add_workdir(skill_validate)

    skill_promote = sub.add_parser("promote-skill", help="验证并把专用 Skill 升级为通用 Skill")
    skill_promote.add_argument("name")
    skill_promote.add_argument("--update", action="store_true", help="更新 Hub 同名 Skill；必须提升 VERSION")
    skill_promote.add_argument("--commit", action="store_true", help="发布后提交 Hub git 仓库")
    _add_workdir(skill_promote)

    mcp_init = sub.add_parser("init-mcp", help="在当前群创建专用 MCP 配置或源码 bundle，不发布")
    mcp_init.add_argument("name")
    transport = mcp_init.add_mutually_exclusive_group(required=True)
    transport.add_argument("--command", default="")
    transport.add_argument("--url", default="")
    mcp_init.add_argument("--bundle", action="store_true", help="创建携带源码/依赖的生成型 bundle（仅 command）")
    _add_workdir(mcp_init)

    mcp_register = sub.add_parser("register-mcp", help="验证并把专用 MCP bundle 写入当前群配置")
    mcp_register.add_argument("name")
    _add_workdir(mcp_register)

    mcp_validate = sub.add_parser("validate-mcp", help="验证当前群专用 MCP")
    mcp_validate.add_argument("name")
    mcp_validate.add_argument("--publish", action="store_true", help="按通用化发布标准检查")
    _add_workdir(mcp_validate)

    mcp_promote = sub.add_parser("promote-mcp", help="验证并把专用 MCP 升级为通用 MCP")
    mcp_promote.add_argument("name")
    mcp_promote.add_argument("--update", action="store_true")
    mcp_promote.add_argument("--commit", action="store_true")
    _add_workdir(mcp_promote)

    system = sub.add_parser("set-system", help="把已发布的通用资源设为或移出系统默认")
    system.add_argument("kind", choices=["skill", "mcp"])
    system.add_argument("name")
    system.add_argument("--remove", action="store_true", help="从系统默认移除，资源仍保留在通用 Hub")
    system.add_argument("--commit", action="store_true")

    return parser


def main() -> None:
    args = build_parser().parse_args()
    wd = _workdir(args) if hasattr(args, "src") else Path.cwd()

    if args.cmd == "list":
        print(json.dumps({
            "system": {"skills": hub.system_skills(), "mcp": hub.system_mcp()},
            "general": {"skills": hub.general_skills(), "mcp": hub.general_mcp()},
            "dedicated": {"skills": hub.list_local_skills(wd), "mcp": hub.list_local_mcp(wd)},
        }, ensure_ascii=False, indent=2))
        return
    if args.cmd == "init-skill":
        path = hub.scaffold_skill(wd, args.name, args.description)
        print(f"✅ 已创建专用 Skill 脚手架：{path}")
        print("请完成 SKILL.md 和所需 resources 后运行 validate-skill；当前尚未发布，也未请求重启。")
        return
    if args.cmd == "validate-skill":
        report = hub.validate_skill(wd, args.name, publish=args.publish)
        _print_report(report)
        raise SystemExit(0 if report.ok else 2)
    if args.cmd == "promote-skill":
        report = hub.publish_skill(wd, args.name, overwrite=args.update)
        _finish_publish(
            report,
            commit=args.commit,
            label=f"skill {args.name}",
            paths=[f"skills/{args.name}"],
        )
        return
    if args.cmd == "init-mcp":
        path = hub.scaffold_mcp(wd, args.name, command=args.command, url=args.url, bundle=args.bundle)
        if path.name == ".mcp.json":
            print(f"✅ 已创建专用 MCP 配置：{path}（无 VERSION）")
            print("请完善对应 server 配置，再运行 register-mcp；当前尚未发布，也未请求重启。")
        else:
            print(f"✅ 已创建专用 MCP 源码 bundle：{path}")
            print(f"请完善 {path / 'mcp.json'} 及源码/测试，再运行 register-mcp；当前尚未发布，也未请求重启。")
        return
    if args.cmd == "register-mcp":
        report = hub.register_local_mcp(wd, args.name)
        _print_report(report)
        raise SystemExit(0 if report.ok else 2)
    if args.cmd == "validate-mcp":
        report = hub.validate_mcp(wd, args.name, publish=args.publish)
        _print_report(report)
        raise SystemExit(0 if report.ok else 2)
    if args.cmd == "promote-mcp":
        report = hub.publish_mcp(wd, args.name, overwrite=args.update)
        _finish_publish(
            report,
            commit=args.commit,
            label=f"mcp {args.name}",
            paths=[f"mcp/{args.name}", f"mcp/{args.name}.json"],
        )
        return
    if args.cmd == "set-system":
        changed = hub.set_system_default(args.kind, args.name, enabled=not args.remove)
        state = "通用可选" if args.remove else "系统默认"
        print(f"✅ {args.kind} {args.name} 当前层级：{state}" + ("" if changed else "（无需变更）"))
        if args.commit and changed:
            print(hub.git_commit(f"hub: set {args.kind} {args.name} {state}", ["defaults.json"]))
        return
if __name__ == "__main__":
    main()
