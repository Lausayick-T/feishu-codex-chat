"""Validation and scaffolding for conversational Skill/MCP lifecycle management."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?$")
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
MAX_SKILL_FILES = 200
MAX_SKILL_FILE_BYTES = 5 * 1024 * 1024
MAX_SKILL_TOTAL_BYTES = 20 * 1024 * 1024
MCP_BUNDLE_TOKEN = "${MCP_BUNDLE_DIR}"

_TEXT_SUFFIXES = {
    "", ".c", ".css", ".go", ".html", ".ini", ".java", ".js", ".json",
    ".jsx", ".md", ".mjs", ".php", ".ps1", ".py", ".rb", ".rs", ".sh",
    ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml", ".zsh",
}
_AUXILIARY_DOCS = {"README.md", "INSTALLATION_GUIDE.md", "QUICK_REFERENCE.md", "CHANGELOG.md"}
_PRIVATE_PATTERNS = (
    (re.compile(r"/(?:Users|home)/[^/\s]+/"), "包含用户绝对路径"),
    (re.compile(r"\bbots/auto_oc_[A-Za-z0-9_-]+"), "包含专用会话工作目录"),
    (re.compile(r"\boc_[A-Za-z0-9_-]{8,}"), "包含飞书会话 ID"),
    (re.compile(r"[A-Za-z]:\\Users\\[^\\\s]+\\"), "包含 Windows 用户绝对路径"),
)
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?:authorization|api[_-]?key|access[_-]?token|secret|password|cookie)"
    r"[\"']?\s*[:=]\s*[\"']([^\"'\r\n]{8,})[\"']"
)


@dataclass
class ValidationReport:
    kind: str
    name: str
    path: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "name": self.name,
            "path": self.path,
            "errors": self.errors,
            "warnings": self.warnings,
            "details": self.details,
        }

    def format(self) -> str:
        head = "✅ 验证通过" if self.ok else "❌ 验证失败"
        lines = [f"{head}: {self.kind} {self.name}"]
        lines.extend(f"  ERROR: {item}" for item in self.errors)
        lines.extend(f"  WARN: {item}" for item in self.warnings)
        if self.details:
            lines.append("  " + json.dumps(self.details, ensure_ascii=False, sort_keys=True))
        return "\n".join(lines)


def validate_name(name: str, label: str = "名称") -> list[str]:
    if not NAME_RE.fullmatch(name):
        return [f"{label}必须是 1-64 位小写字母、数字或连字符，且不能以连字符开头/结尾"]
    return []


def _frontmatter(text: str) -> dict[str, str] | None:
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    values: dict[str, str] = {}
    for raw in text[4:end].splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values


def _is_placeholder(value: str) -> bool:
    stripped = value.strip()
    upper = stripped.upper()
    return (
        not stripped
        or "${" in stripped
        or re.fullmatch(r"\$[A-Z][A-Z0-9_]*", stripped) is not None
        or upper.startswith(("ENV:", "FROM_ENV:", "REPLACE_ME", "YOUR_", "<"))
    )


def _scan_text(text: str, relative: str, report: ValidationReport, publish: bool) -> None:
    for pattern, label in _PRIVATE_PATTERNS:
        if pattern.search(text):
            target = report.errors if publish else report.warnings
            target.append(f"{relative}: {label}，请改为参数或相对路径")
    for match in _SECRET_ASSIGNMENT_RE.finditer(text):
        value = match.group(1)
        if not _is_placeholder(value):
            target = report.errors if publish else report.warnings
            target.append(f"{relative}: 疑似包含明文密钥或令牌")
            break


def validate_skill_dir(path: Path, name: str, *, publish: bool = False) -> ValidationReport:
    report = ValidationReport("skill", name, str(path))
    report.errors.extend(validate_name(name, "Skill 名称"))
    if not path.is_dir():
        report.errors.append("Skill 目录不存在")
        return report
    if path.is_symlink():
        report.errors.append("Skill 根目录不能是软链接")
        return report

    files = [item for item in path.rglob("*") if item.is_file() or item.is_symlink()]
    total = 0
    if len(files) > MAX_SKILL_FILES:
        report.errors.append(f"文件数超过限制：{len(files)} > {MAX_SKILL_FILES}")
    for item in files:
        rel = item.relative_to(path).as_posix()
        if item.is_symlink():
            report.errors.append(f"不允许软链接：{rel}")
            continue
        size = item.stat().st_size
        total += size
        if size > MAX_SKILL_FILE_BYTES:
            report.errors.append(f"单个文件超过 5MB：{rel}")
        if item.name in _AUXILIARY_DOCS:
            report.warnings.append(f"{rel}: 建议移除非必要辅助文档，将内容放进 SKILL.md 或 references/")
        if item.suffix.lower() in _TEXT_SUFFIXES and size <= 1024 * 1024:
            try:
                _scan_text(item.read_text(encoding="utf-8"), rel, report, publish)
            except UnicodeDecodeError:
                pass
    if total > MAX_SKILL_TOTAL_BYTES:
        report.errors.append(f"Skill 总大小超过 20MB：{total} bytes")

    skill_md = path / "SKILL.md"
    if not skill_md.is_file():
        report.errors.append("缺少 SKILL.md")
    else:
        text = skill_md.read_text(encoding="utf-8")
        meta = _frontmatter(text)
        if meta is None:
            report.errors.append("SKILL.md 缺少有效 YAML frontmatter")
        else:
            if meta.get("name") != name:
                report.errors.append(f"frontmatter name 必须与目录名一致：{name}")
            if not meta.get("description"):
                report.errors.append("frontmatter description 不能为空")
            extras = sorted(set(meta) - {"name", "description"})
            if extras:
                report.warnings.append("frontmatter 建议只保留 name 和 description：" + ", ".join(extras))
        if len(text.splitlines()) > 500:
            report.warnings.append("SKILL.md 超过 500 行，建议把细节拆到 references/")
        if "TODO" in text:
            report.errors.append("SKILL.md 仍包含 TODO，尚未完成")

    version_path = path / "VERSION"
    version = ""
    if not version_path.is_file():
        report.errors.append("缺少 VERSION")
    else:
        version = version_path.read_text(encoding="utf-8").strip().splitlines()[0]
        if not SEMVER_RE.fullmatch(version):
            report.errors.append("VERSION 必须是有效 SemVer，例如 0.1.0")

    if not (path / "agents" / "openai.yaml").is_file():
        report.warnings.append("缺少推荐的 agents/openai.yaml UI 元数据")
    report.details = {"version": version, "files": len(files), "bytes": total, "publish": publish}
    return report


def _normalize_mcp_document(document: object) -> dict:
    if not isinstance(document, dict):
        return {}
    servers = document.get("mcpServers")
    return servers if isinstance(servers, dict) else document


def load_mcp_document(path: Path) -> dict:
    try:
        return _normalize_mcp_document(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return {}


def _scan_secret_mapping(mapping: object, prefix: str, report: ValidationReport, publish: bool) -> None:
    if not isinstance(mapping, dict):
        report.errors.append(f"{prefix} 必须是对象")
        return
    for key, raw in mapping.items():
        if not isinstance(raw, (str, int, float, bool)):
            report.errors.append(f"{prefix}.{key} 必须是标量")
            continue
        value = str(raw)
        if re.search(r"(?i)(authorization|token|secret|password|api[_-]?key|cookie)", str(key)) and not _is_placeholder(value):
            report.warnings.append(
                f"{prefix}.{key} 包含明文凭据；允许用于受信任的本机共享 Hub，请勿导出到公共仓库"
            )


def validate_mcp_definition(
    name: str,
    document: object,
    *,
    path: Path | None = None,
    bundle_dir: Path | None = None,
    publish: bool = False,
) -> ValidationReport:
    report = ValidationReport("mcp", name, str(path or ""))
    report.errors.extend(validate_name(name, "MCP 名称"))
    servers = _normalize_mcp_document(document)
    if not servers:
        report.errors.append("MCP 配置为空或不是 JSON 对象")
        return report
    # A library resource may group multiple MCP servers under one display name,
    # as a shared multi-server resource does. Single-server resources still keep
    # the stricter name-to-server-key match so typos are caught.
    if name not in servers and len(servers) == 1:
        report.errors.append(f"配置必须包含与资源名一致的 server key：{name}")

    for key, conf in servers.items():
        report.errors.extend(validate_name(str(key), "MCP server key"))
        if not isinstance(conf, dict):
            report.errors.append(f"{key}: server 配置必须是对象")
            continue
        has_url = bool(conf.get("url"))
        has_command = bool(conf.get("command"))
        if has_url == has_command:
            report.errors.append(f"{key}: 必须且只能设置 url 或 command 之一")
        if has_url:
            parsed = urlparse(str(conf["url"]))
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                report.errors.append(f"{key}: url 必须是有效的 http/https 地址")
        if has_command and not isinstance(conf.get("command"), str):
            report.errors.append(f"{key}: command 必须是字符串")
        if "args" in conf and (
            not isinstance(conf["args"], list) or not all(isinstance(item, str) for item in conf["args"])
        ):
            report.errors.append(f"{key}: args 必须是字符串数组")
        for timeout_key in ("startup_timeout_sec", "tool_timeout_sec"):
            if timeout_key in conf and (
                not isinstance(conf[timeout_key], (int, float)) or isinstance(conf[timeout_key], bool) or conf[timeout_key] <= 0
            ):
                report.errors.append(f"{key}: {timeout_key} 必须是正数")
        for map_key in ("env", "headers", "http_headers"):
            if map_key in conf:
                _scan_secret_mapping(conf[map_key], f"{key}.{map_key}", report, publish)

        serialized = json.dumps(conf, ensure_ascii=False)
        for pattern, label in _PRIVATE_PATTERNS:
            if pattern.search(serialized):
                target = report.errors if publish else report.warnings
                target.append(f"{key}: {label}，通用 MCP 必须使用 {MCP_BUNDLE_TOKEN} 或参数")
        if publish and MCP_BUNDLE_TOKEN not in serialized:
            for value in [conf.get("command"), *(conf.get("args") or [])]:
                if isinstance(value, str) and Path(value).is_absolute():
                    report.errors.append(f"{key}: 通用 MCP 不能引用绝对可执行文件路径：{value}")

        if bundle_dir and MCP_BUNDLE_TOKEN in serialized:
            for value in [conf.get("command"), *(conf.get("args") or [])]:
                if not isinstance(value, str) or MCP_BUNDLE_TOKEN not in value:
                    continue
                candidate = Path(value.replace(MCP_BUNDLE_TOKEN, str(bundle_dir)))
                if not candidate.exists():
                    try:
                        display = candidate.relative_to(bundle_dir)
                    except ValueError:
                        display = candidate
                    report.errors.append(f"{key}: bundle 引用不存在：{display}")

    if bundle_dir:
        if bundle_dir.is_symlink():
            report.errors.append("MCP bundle 根目录不能是软链接")
        for item in bundle_dir.rglob("*"):
            if item.is_symlink():
                report.errors.append(f"不允许软链接：{item.relative_to(bundle_dir)}")
        version_path = bundle_dir / "VERSION"
        version = ""
        if not version_path.is_file():
            report.errors.append("MCP bundle 缺少 VERSION")
        else:
            version = version_path.read_text(encoding="utf-8").strip().splitlines()[0]
            if not SEMVER_RE.fullmatch(version):
                report.errors.append("MCP bundle VERSION 必须是有效 SemVer，例如 0.1.0")
    else:
        version = ""
    report.details = {
        "servers": sorted(servers),
        "bundle": bool(bundle_dir),
        "publish": publish,
        "version": version,
    }
    return report


def scaffold_skill(workdir: Path, name: str, description: str) -> Path:
    errors = validate_name(name, "Skill 名称")
    if errors:
        raise ValueError(errors[0])
    if not description.strip():
        raise ValueError("Skill description 不能为空")
    dst = workdir / ".codex" / "skills" / name
    if dst.exists():
        raise FileExistsError(f"Skill 已存在：{dst}")
    (dst / "agents").mkdir(parents=True)
    display_name = " ".join(part.capitalize() for part in name.split("-"))
    skill_md = (
        "---\n"
        f"name: {name}\n"
        f"description: {json.dumps(description.strip(), ensure_ascii=False)}\n"
        "---\n\n"
        f"# {display_name}\n\n"
        "## Workflow\n\n"
        "TODO: Replace this line with concise, imperative instructions and add only the reusable resources required.\n"
    )
    openai_yaml = (
        "interface:\n"
        f"  display_name: {json.dumps(display_name, ensure_ascii=False)}\n"
        f"  short_description: {json.dumps(description.strip()[:64], ensure_ascii=False)}\n"
        f"  default_prompt: {json.dumps(f'Use ${name} to complete this task.', ensure_ascii=False)}\n"
    )
    (dst / "SKILL.md").write_text(skill_md, encoding="utf-8")
    (dst / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    (dst / "agents" / "openai.yaml").write_text(openai_yaml, encoding="utf-8")
    return dst


def scaffold_mcp(
    workdir: Path,
    name: str,
    *,
    command: str = "",
    url: str = "",
    bundle: bool = False,
) -> Path:
    errors = validate_name(name, "MCP 名称")
    if errors:
        raise ValueError(errors[0])
    if bool(command) == bool(url):
        raise ValueError("必须且只能提供 command 或 url")
    conf: dict = {"url": url, "type": "http"} if url else {"command": command, "args": []}
    if not bundle:
        dst = workdir / ".mcp.json"
        try:
            document = json.loads(dst.read_text(encoding="utf-8")) if dst.is_file() else {}
        except json.JSONDecodeError as exc:
            raise ValueError(f"现有 .mcp.json 不是有效 JSON：{exc}") from exc
        if not isinstance(document, dict):
            raise ValueError("现有 .mcp.json 必须是 JSON 对象")
        servers = document.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError("现有 .mcp.json 的 mcpServers 必须是对象")
        if name in servers:
            raise FileExistsError(f"MCP 配置已存在：{name}")
        servers[name] = conf
        dst.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return dst

    if url:
        raise ValueError("HTTP URL MCP 是配置型资源，不需要 bundle")
    dst = workdir / ".codex" / "mcp" / name
    if dst.exists():
        raise FileExistsError(f"MCP bundle 已存在：{dst}")
    dst.mkdir(parents=True)
    (dst / "mcp.json").write_text(json.dumps({name: conf}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (dst / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    return dst
