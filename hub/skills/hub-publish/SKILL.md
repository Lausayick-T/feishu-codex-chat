---
name: hub-publish
description: 通过当前飞书对话创建、修改、验证、启用和发布 Codex 专用或通用 Skill/MCP。用户要求创建 Skill、制作 MCP、修改现有资源、测试专用资源、升级为通用、发布到 Hub 或应用资源配置时使用。
---

# 对话式 Skill / MCP 生命周期

把当前飞书会话作为资源开发环境。默认创建“专用”资源；只有用户明确要求“升级为通用”“发布到 Hub”时才发布共享资源。

使用 `$CHAT_AGENT_HOME/hub_cli.py` 执行确定性的创建、验证、注册和发布操作。所有命令默认读取当前工作目录。

## 创建专用 Skill

1. 从用户需求提炼 2-4 个具体触发示例和完成标准。信息足够时直接继续，不重复确认。
2. 使用小写连字符名称创建脚手架：

   ```bash
   python "$CHAT_AGENT_HOME/hub_cli.py" init-skill <name> --description '<触发条件与能力说明>'
   ```

3. 完成 `.codex/skills/<name>/`：
   - `SKILL.md`：frontmatter 只保留 `name`、`description`，正文使用祈使式工作流。
   - `VERSION`：新资源从 `0.1.0` 开始。
   - `agents/openai.yaml`：保持显示名、短描述和包含 `$<name>` 的默认提示准确。
   - 仅在确有复用价值时添加 `scripts/`、`references/`、`assets/`；不要添加 README、安装指南或变更日志。
4. 实际运行新增脚本和代表性用例。
5. 验证：

   ```bash
   python "$CHAT_AGENT_HOME/hub_cli.py" validate-skill <name>
   ```

6. 修复所有 ERROR。资源此时只属于当前群，不要执行 `promote-skill`。

## 更新专用 Skill

直接修改 `.codex/skills/<name>/`，运行测试和 `validate-skill`。根据 SemVer 提升 `VERSION`：兼容修复用 patch，兼容新增能力用 minor，破坏性变更用 major。

## 创建专用 MCP

区分两种 MCP：

- 配置已有 stdio/HTTP 服务：作为配置型 MCP 写入 `.mcp.json`，不创建目录，不使用 `VERSION`。
- 从零生成 MCP server：把服务源码、依赖声明、测试和配置模板放进 `.codex/mcp/<name>/` bundle，并使用 `VERSION`。

创建脚手架：

```bash
python "$CHAT_AGENT_HOME/hub_cli.py" init-mcp <name> --command python3
# 或
python "$CHAT_AGENT_HOME/hub_cli.py" init-mcp <name> --url https://example.com/mcp
# 只有需要携带自建服务源码时：
python "$CHAT_AGENT_HOME/hub_cli.py" init-mcp <name> --command python3 --bundle
```

配置型 MCP 直接编辑 `.mcp.json` 中对应 server；发布后保存为 Hub 的单个 `<name>.json`。个人 MCP 配置只保留在本机 Hub，不要提交到公开源码仓库。

生成型 bundle 位于 `.codex/mcp/<name>/`，至少包含 `mcp.json` 和 `VERSION`。配置使用可移植路径，例如：

```json
{
  "research-tools": {
    "command": "python3",
    "args": ["${MCP_BUNDLE_DIR}/server.py"],
    "startup_timeout_sec": 30,
    "tool_timeout_sec": 120
  }
}
```

实现后依次执行：

```bash
python "$CHAT_AGENT_HOME/hub_cli.py" register-mcp <name>
python "$CHAT_AGENT_HOME/hub_cli.py" validate-mcp <name>
```

能直接运行 MCP 时，验证启动、`initialize`、`tools/list` 和至少一个无副作用工具调用。不能完成真实握手时，明确说明未验证部分，不要声称连接成功。

## 升级为通用资源

仅在用户明确要求后执行。先完成通用化：

- 删除群 ID、用户姓名、个人目录、绝对路径和特定数据。
- 把环境差异改为参数、相对路径、`${MCP_BUNDLE_DIR}` 或环境变量引用。
- 删除样本中的私人数据。
- Skill 及生成型 MCP 的源码、说明和样例中不得夹带真实凭据。
- 本机共享 Hub 的配置型 MCP 可按用户要求直接保存 `Authorization`、Token 或 API key；这是受信任的本机配置，不强制改成环境变量，也不要声称因此无法发布。不要把带明文凭据的配置导出到公共仓库。
- 确保名称、描述、使用说明和默认提示适用于其他群。
- 更新 Hub 已有同名 Skill 或生成型 MCP bundle 时先提升 `VERSION`；配置型 MCP 没有 `VERSION`，使用 `--update` 覆盖。

按发布标准预检：

```bash
python "$CHAT_AGENT_HOME/hub_cli.py" validate-skill <name> --publish
python "$CHAT_AGENT_HOME/hub_cli.py" validate-mcp <name> --publish
```

只执行与资源类型对应的一条。修复所有 ERROR 后发布：

```bash
python "$CHAT_AGENT_HOME/hub_cli.py" promote-skill <name> --commit
python "$CHAT_AGENT_HOME/hub_cli.py" promote-mcp <name> --commit
```

Hub 已有同名资源且用户要求更新时增加 `--update`。发布后资源属于“通用”，不会自动变成所有群默认安装。

只有用户明确要求“设为系统默认”时才提升层级：

```bash
python "$CHAT_AGENT_HOME/hub_cli.py" set-system skill <name> --commit
python "$CHAT_AGENT_HOME/hub_cli.py" set-system mcp <name> --commit
```

只执行对应类型。用户要求恢复为通用可选时增加 `--remove`。这个操作影响新群及其他群的下次启动，不要擅自重启所有群。

## 应用边界

- Skill 新增和修改由 Codex 自动检测，通常无需重启。
- MCP 注册和修改只更新当前群配置并标记“待载入”，不得从对话内请求或执行重启。
- 用户打开控制面板时会看到资源更新提示；只有用户点击“重启当前会话并载入”后，chat-agent 才重启该群对应的 Codex 会话。
- 发布到 Hub 后，其他群只同步文件和显示控制面板提示，不得自动重启任何群。

## 汇报

最终只说明：创建/更新了什么、当前是专用还是通用、验证结果、Skill 是否已被动态检测、MCP 是否等待用户从控制面板载入，以及尚未完成的真实连接测试。不要把“文件已生成”等同于“Skill 已检测”或“MCP 已连接”。
