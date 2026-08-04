# 从零部署指南

这是一条从空白机器到飞书中收到 Codex 回复的完整路径。请按顺序操作；每一步都有验证点。第一次部署不需要配置 MCP，也不需要多维表格。

## 0. 开始前确认

你需要：

- 一台持续在线的 macOS 或 Linux 机器；原生 Windows 暂不支持。
- 能创建飞书企业自建应用的账号，或一位能代为审核、发布应用的管理员。
- 可以使用 Codex 的 ChatGPT 账号，或可用的 OpenAI API Key。
- 机器可以访问飞书开放平台与 Codex 所需网络服务。

建议使用专用、低权限的系统账户运行。默认配置会让 Codex 以无人值守模式工作，因此这个账户不应能读取个人 SSH 私钥、浏览器资料、云凭证或无关业务目录。

## 1. 安装本机依赖

### macOS

先安装 [Homebrew](https://brew.sh/)，再执行：

```bash
brew install python@3.12 uv tmux git
```

### Ubuntu / Debian

```bash
sudo apt update
sudo apt install -y git tmux curl python3 python3-venv
curl -LsSf https://astral.sh/uv/install.sh | sh
```

如果安装 `uv` 后当前终端仍找不到它，重新打开终端，或按安装器提示加载 shell 配置。其他 Linux 发行版请用系统包管理器安装 Python 3.10+、Git、tmux 和 curl，再按 [uv 官方安装说明](https://docs.astral.sh/uv/getting-started/installation/)安装 `uv`。

### 安装 Codex CLI

按 Codex CLI 官方当前安装方式执行：

```bash
curl -fsSL https://chatgpt.com/codex/install.sh | sh
```

然后验证版本并登录：

```bash
codex --version
codex login
codex login status
```

`codex login` 会引导浏览器登录。如果使用 API Key，避免把 Key 直接写进命令或 shell 历史：

```bash
read -s OPENAI_API_KEY
export OPENAI_API_KEY
printenv OPENAI_API_KEY | codex login --with-api-key
unset OPENAI_API_KEY
codex login status
```

Codex 会在本机保存登录状态。请像保护密码一样保护运行账户及其 Codex 凭证存储。安装和登录方式如有变化，以 [Codex CLI 官方文档](https://developers.openai.com/codex/cli)为准。

### 本步验证

```bash
python3 --version
uv --version
tmux -V
git --version
codex login status
```

Python 必须是 3.10 或更高版本，最后一条应显示已登录。

## 2. 下载并安装项目

```bash
git clone https://github.com/Lausayick-T/feishu-codex-chat.git
cd feishu-codex-chat
uv sync --locked
```

`uv sync --locked` 会在项目内创建 `.venv` 并严格按锁文件安装依赖。成功后验证核心 Python 依赖：

```bash
uv run python -c "import lark_oapi; print('Python 依赖安装成功')"
```

完整 `doctor` 需要飞书凭证，因此放在第 3 步执行。后续诊断中出现 `Codex 权限` 的黄色提醒，表示当前使用无人值守高权限模式；它不是启动失败，但必须认真处理前面的账户隔离建议。

## 3. 创建飞书应用

1. 打开[飞书开放平台开发者后台](https://open.feishu.cn/app)，创建“企业自建应用”。
2. 在“添加应用能力”中添加“机器人”。
3. 进入“凭证与基础信息”，复制 App ID 和 App Secret。

不要把 App Secret 发到群聊、截图、Issue 或终端命令参数中。

回到项目目录：

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

也可以用你熟悉的编辑器。只填写这两个必填项，把等号右侧替换成刚才复制的真实值：

```dotenv
FEISHU_APP_ID=
FEISHU_APP_SECRET=
```

首次部署保持下面两个可选项为空：

```dotenv
FEISHU_BITABLE_APP_TOKEN=
FEISHU_BITABLE_TABLE_ID=
```

保存后验证凭证，但诊断不会打印凭证内容：

```bash
./start.sh doctor
```

期望结果是 `0 项失败`。如果“机器人能力”只是提醒，可先完成下一步的机器人配置与发布，再回来重试。

## 4. 配置飞书权限

在飞书应用的“权限管理”中，以“应用身份”申请以下推荐权限：

| 权限标识 | 用途 |
|---|---|
| `im:message` | 发送、更新、撤回消息，并下载收到的消息资源 |
| `im:message.p2p_msg:readonly` | 接收私聊消息 |
| `im:message.group_at_msg:readonly` | 接收群内 `@机器人` 消息 |
| `im:message.group_msg` | 接收群普通消息，支持单真人群免 `@`；敏感权限 |
| `im:chat.members:read` | 读取群成员人数，判断单真人群或多人群 |
| `im:chat.members:bot_access` | 接收机器人进群事件并发送欢迎语 |
| `im:resource` | 上传和发送图片、文件、音频、视频 |

如果企业不允许综合权限 `im:message`，请按[飞书权限清单](feishu-permissions.md)换成细粒度组合。第一次部署不要申请多维表格权限；那是可选控制台功能。

权限申请获批后，通常还需要创建并发布新版本才会对线上应用生效。

## 5. 先启动长连接客户端

保持当前终端可见，启动两个服务：

```bash
./start.sh
```

这是首次部署最容易观察错误的方式。正常情况下会看到 Server 和 Scheduler 启动，Server 随后尝试连接飞书长连接。不要关闭这个终端。

如果飞书后台允许先保存长连接配置，也可以先完成下一步再启动；如果后台提示“应用未建立长连接”，就保持本命令运行，再刷新并保存配置。

## 6. 配置事件与卡片回调

回到飞书开发者后台的“事件与回调”。不同版本后台的菜单文字可能略有变化，但要分别完成两件事。

### 事件配置

1. 订阅方式选择“使用长连接接收事件”。
2. 添加必需事件“接收消息 v2.0”：`im.message.receive_v1`。
3. 添加推荐事件“机器人进群”：`im.chat.member.bot.added_v1`。

### 回调配置

1. 投递方式选择“使用长连接接收回调”。
2. 添加“卡片回传交互”：`card.action.trigger`。

事件和回调是两个独立页面。只添加消息事件时，机器人可能能回复文字，但控制面板、停止、重启和模型选择按钮都不会响应。

更完整的选项见[飞书应用配置](feishu-setup.md)和[飞书权限清单](feishu-permissions.md)。

## 7. 发布应用并加入会话

1. 创建一个应用版本，并在版本说明中写清机器人用途。
2. 设置应用可用范围，确保测试用户在范围内。
3. 提交审核并发布；如果你没有管理员权限，请管理员完成这一步。
4. 在飞书中找到机器人并发起私聊。
5. 需要群聊时，将机器人添加到目标群。

权限、事件或回调有任何修改，都应确认新版本已发布，而不只是保存在开发者后台。

## 8. 首次验收

服务仍以前台方式运行时，按顺序测试：

1. 私聊机器人发送 `/help`：应收到控制面板卡片。
2. 点击一个无破坏性的控制面板按钮：卡片应更新，证明回调可用。
3. 私聊发送“只回复：连接成功”：应收到 Codex 回复。
4. 发送 `/status`：检查飞书、Codex 和 Scheduler 状态。
5. 发送一张图片或小文件，再说明处理要求：应能读取附件。
6. 在多人群中先发普通消息，再 `@机器人`：普通消息应忽略，`@` 后应回复。
7. 发送 `/privacy`：应看到当前会话在本机保存的数据类型与清理入口。

首次收到新会话消息时，项目会自动创建 `bots/auto_<chat_id>/` 和本机 `registry.json`。这些文件都被 `.gitignore` 排除。

如果任一步失败，先保持服务运行并打开[故障排查](troubleshooting.md)，按现象定位。

## 9. 切换为后台运行

验收通过后，在前台终端按 `Ctrl-C` 同时停止 Server 和 Scheduler，再选择一种运行方式。建议先用 tmux：

```bash
./start.sh tmux start
./start.sh tmux status
./start.sh tmux logs
```

`status` 应显示 `feishu-codex-chat` 正在运行，并列出 `server` 和 `scheduler` 两个窗口。再次发送 `/status` 做最终确认。

如果这台机器需要登录后自动恢复服务：

```bash
./start.sh tmux stop
./start.sh autostart install
./start.sh autostart status
```

四种启动方式只能选一种，不能同时运行前台、tmux、nohup 和 autostart。日常维护方式见[运行与维护](operations.md)。

## 10. 部署完成清单

- [ ] `codex login status` 显示已登录。
- [ ] `./start.sh doctor` 为 `0 项失败`。
- [ ] `.env` 权限为 `600`，且 `git status --short` 不显示 `.env`。
- [ ] 飞书机器人能力已启用。
- [ ] 核心 API 权限已批准且应用新版本已发布。
- [ ] `im.message.receive_v1` 使用长连接。
- [ ] `card.action.trigger` 回调也使用长连接。
- [ ] 私聊 `/help`、普通问题、卡片按钮和附件均通过。
- [ ] 多人群仅在 `@机器人` 时回复。
- [ ] 后台服务的 `status`、`logs` 均正常。
- [ ] 运行账户无法访问无关的个人或生产凭证。

以上全部完成，才算部署成功。
