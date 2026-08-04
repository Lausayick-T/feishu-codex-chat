# 安全与隐私

## 凭证

- 真实凭证只保存在 `.env` 或服务进程环境中。
- `.env` 权限建议为 `600`，服务使用专用低权限系统账户。
- `config.json` 只保存 `${ENV_VAR}` 引用；个人 `hub/mcp/*.json` 配置只留在本机且被 Git 忽略。
- 不要把 Token 作为命令行参数；命令行可能被 `ps` 或进程监控读取。
- 一旦凭证出现在 Git 历史、公开日志或聊天中，应立即在服务端轮换。

## 日志

`utils/config.py` 会给 Python logging 安装脱敏工厂，隐藏：

- URL 中的 `access_key`、`ticket`、`token` 等查询参数。
- `Authorization: Bearer ...`。
- 名称包含 secret、password、token、API key、cookie 等的赋值。
- 当前进程环境中已知的敏感变量值。

`state/` 和 `*.log` 不会进入 Git。仍应限制日志文件权限、配置保留周期，并避免在自定义 Skill 中直接打印环境变量或 HTTP 请求头。

## Codex 权限边界

默认命令包含跳过审批和沙箱的参数，用于无人值守运行。这是高风险配置：

- 使用专用操作系统账户。
- 工作目录只指向 `bots/<conversation>/`，不要指向 Home、仓库根目录或共享文件区。
- 不要让该账户读取 SSH 私钥、浏览器数据、云凭证或其他应用配置。
- 仅安装可信 Skill/MCP；发布校验不能替代代码审查。
- 对外部上传文件按不可信输入处理。

## Git 发布前检查

至少确认：

```bash
git status --short
git ls-files
git grep -n -I -E '(Bearer [A-Za-z0-9]|app_secret.*[^$][A-Za-z0-9]{8}|BEGIN .*PRIVATE KEY)'
uv run python scripts/audit_public.py
```

推荐再使用 `gitleaks` 或 `trufflehog` 扫描完整 Git 历史。发现泄漏后，应先轮换凭证，再清理历史。

## 不进入仓库的数据

`.env`、`.mcp.json`、`registry.json`、`state/`、整个 `bots/*`、个人 MCP、非内置 Skill、Agent 本机配置、附件、记忆、生成报告、JSONL 会话、SQLite 数据库和日志都属于本机数据。

飞书用户可发送 `/privacy` 查看当前会话数据。附件和定时任务日志默认保留 30 天；摘要记忆与生成文件只允许手动清理，所有清理操作均限制在当前会话目录并要求二次确认。
