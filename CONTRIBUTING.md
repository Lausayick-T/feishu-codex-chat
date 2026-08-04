# 贡献指南

感谢参与 Feishu Codex Chat。

提交前请确认：

1. 不提交 `.env`、真实飞书 ID、聊天内容、附件、日志、数据库、个人 MCP 或非内置 Skill。
2. 新增配置使用空白示例或 `${ENV_VAR}`，不要写入真实地址和凭证。
3. 运行 `uv sync --locked`、`uv run python -m unittest discover -s tests -v`、`uv run python scripts/check_docs.py` 和 `uv run python scripts/audit_public.py`。
4. 涉及删除本地数据、重启服务或外部写操作时，必须有清晰提示和二次确认。

报告问题时请先删除截图、日志和复现内容中的 App ID、Token、群 ID、用户 ID、本机路径及对话正文。
