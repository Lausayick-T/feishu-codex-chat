# 飞书权限、事件与回调清单

本清单按当前代码实际调用的 OpenAPI 和注册的事件整理。飞书把“API 权限”“事件订阅”“卡片回调”分成三套配置，三者不能互相替代。

## 推荐方案：使用综合消息权限

这是配置最简单、最不容易漏接口的一组应用身份权限：

| 权限标识 | 飞书后台名称 | 本项目用途 | 必需性 |
|---|---|---|---|
| `im:message` | 获取与发送单聊、群组消息 | 发送文本/图片/文件/卡片，更新和撤回机器人消息，下载消息附件 | 核心必需 |
| `im:message.p2p_msg:readonly` | 读取用户发给机器人的单聊消息 | 接收私聊消息事件 | 私聊必需 |
| `im:message.group_at_msg:readonly` | 接收群聊中 @ 机器人消息事件 | 接收多人群中的用户 `@机器人` 消息 | 群聊必需 |
| `im:message.group_msg` | 获取群组中所有消息（敏感权限） | 单真人群免 `@`；读取群中普通消息 | 推荐；不开则群聊只能 `@` |
| `im:chat.members:read` | 查看群成员 | 调用群成员列表接口，区分单真人群和多人群 | 推荐 |
| `im:chat.members:bot_access` | 订阅机器人进、出群事件 | 接收机器人进群事件，自动创建工作区并发送欢迎语 | 推荐 |
| `im:resource` | 获取与上传图片或文件资源 | 上传图片、文件、音频或视频后再通过消息发送 | 附件发送必需 |

`im:message` 是综合权限，覆盖本项目使用的发送、更新、撤回以及消息资源下载接口。它不会自动授予消息事件推送能力，因此私聊、群 `@` 和群全部消息权限仍需单独申请。

## 最小粒度方案

如果企业不允许申请综合的 `im:message`，可以改为下面的细粒度组合：

| 权限标识 | 用途 |
|---|---|
| `im:message:send_as_bot` | 以机器人身份发送文本、富文本、图片、文件、音视频和交互卡片 |
| `im:message:update` | 原地更新已发送的进度卡和最终回复卡 |
| `im:message:recall` | 撤回机器人自己发送的卡片或消息 |
| `im:message:readonly` | 下载收到的消息附件和读取消息相关资源 |
| `im:resource` | 上传待发送的图片和文件 |

再叠加上一节的三个接收消息权限和群成员权限：

- `im:message.p2p_msg:readonly`
- `im:message.group_at_msg:readonly`
- `im:message.group_msg`（需要免 `@` 时）
- `im:chat.members:read`
- `im:chat.members:bot_access`

飞书接口通常写的是“开启任一权限即可”，因此不要同时申请 `im:message` 和所有细粒度消息权限；二选一即可。

## 代码功能与权限映射

| 代码行为 | OpenAPI / 事件 | 权限 |
|---|---|---|
| 发送文字、图片、文件、音频、视频、交互卡片 | `POST /im/v1/messages` | `im:message` 或 `im:message:send_as_bot` |
| 更新进度卡和最终卡片 | `PATCH /im/v1/messages/:message_id` | `im:message`、`im:message:send_as_bot` 或 `im:message:update` 任一 |
| 撤回控制面板卡片 | `DELETE /im/v1/messages/:message_id` | `im:message`、`im:message:send_as_bot` 或 `im:message:recall` 任一 |
| 上传图片 | `POST /im/v1/images` | `im:resource` |
| 上传文件 | `POST /im/v1/files` | `im:resource` |
| 下载消息中的图片/文件/音视频 | `GET /im/v1/messages/:message_id/resources/:file_key` | `im:message` 或 `im:message:readonly` |
| 读取群成员人数 | `GET /im/v1/chats/:chat_id/members` | `im:chat.members:read`；飞书也允许部分群信息类权限，但本项目建议直接申请这一项 |
| 接收私聊 | `im.message.receive_v1` | `im:message.p2p_msg:readonly` |
| 接收群内 `@机器人` | `im.message.receive_v1` | `im:message.group_at_msg:readonly` |
| 接收群内所有用户消息 | `im.message.receive_v1` | `im:message.group_msg` |
| 机器人被拉入群 | `im.chat.member.bot.added_v1` | `im:chat.members:bot_access` |

获取机器人自身信息 `GET /bot/v3/info` 和获取 `tenant_access_token` 不需要另外申请业务 scope，但应用必须启用机器人能力并处于可用状态。

## 卡片回调

卡片点击对应 `card.action.trigger` 回调。该回调本身没有单独的 API scope，但必须在开发者后台完成配置：

1. 进入“事件与回调 → 回调配置”。
2. 订阅卡片回传交互 `card.action.trigger`。
3. 投递方式选择“使用长连接接收回调”。
4. 保证服务在 3 秒内返回响应；耗时操作应先响应，再由后台更新卡片。

只配置 `im.message.receive_v1` 事件并不能收到卡片按钮回调。事件订阅和回调订阅是两个独立页面。

## 事件订阅

在“事件与回调 → 事件配置”中选择长连接，并按启用功能订阅：

| 事件 | 事件类型 | 是否需要 |
|---|---|---|
| 接收消息 v2.0 | `im.message.receive_v1` | 必需 |
| 机器人进群 | `im.chat.member.bot.added_v1` | 推荐 |
| 消息撤回 | `im.message.recalled_v1` | 可选；当前代码仅静默处理 |
| 多维表格记录变更 | `drive.file.bitable_record_changed_v1` | 启用多维表格控制台时需要 |

长连接只决定事件如何送达，不能代替对应权限。

## 多维表格控制台（可选）

当前代码会读取、创建和修改数据表、字段与记录，因此只读权限不够。建议申请：

| 权限标识 | 飞书后台名称 | 用途 |
|---|---|---|
| `bitable:app` | 查看、评论、编辑和管理多维表格 | 读取/新增/更新表、字段和记录 |
| `space:document.event:read` | 订阅云文档事件 | 接收多维表格记录变更事件；后台若区分身份，应按官方要求配置相应应用身份权限 |

此外还要满足资源授权条件：

1. 目标多维表格必须允许该应用访问。非应用自有表格通常需要在表格中“添加文档应用”。
2. 填写 `.env` 中的 `FEISHU_BITABLE_APP_TOKEN` 和 `FEISHU_BITABLE_TABLE_ID`。
3. 在开发者后台添加 `drive.file.bitable_record_changed_v1` 事件。
4. 按飞书云文档事件机制订阅目标多维表格；仅注册事件处理函数并不会自动获得文档访问权。

不使用多维表格控制台时，不要申请这两项权限，也不要填写 `FEISHU_BITABLE_*`。

## 不需要申请的权限

本项目不会主动拉人进群、修改群资料、读取通讯录或访问云盘，因此不需要：

- `im:chat.members:write_only`
- `im:chat:update`
- `contact:*`
- 普通云盘文件管理权限
- 用户身份 `user_access_token` 权限

## 发布检查

1. 启用机器人能力。
2. 申请上述权限，身份选择“应用身份”。
3. 配置事件长连接和卡片回调长连接。
4. 创建新版本并发布；仅勾选权限但未发布通常不会对线上版本生效。
5. 检查应用可用范围，确保目标私聊用户和群成员在范围内。
6. 把机器人加入目标群；获取群成员和下载消息资源都要求机器人位于对应会话中。

## 官方资料

- [API 权限列表](https://open.feishu.cn/document/server-docs/application-scope/scope-list)
- [接收消息事件](https://open.feishu.cn/document/server-docs/im-v1/message/events/receive)
- [发送消息](https://open.feishu.cn/document/server-docs/im-v1/message/create)
- [消息与卡片 API 概述](https://open.feishu.cn/document/server-docs/im-v1/introduction)
- [获取群成员列表](https://open.feishu.cn/document/server-docs/group/chat-member/get)
- [上传文件](https://open.feishu.cn/document/server-docs/im-v1/file/create)
- [上传图片](https://open.feishu.cn/document/server-docs/im-v1/image/create)
- [获取消息中的资源文件](https://open.feishu.cn/document/server-docs/im-v1/message/get-2)
- [处理回调](https://open.feishu.cn/document/event-subscription-guide/callback-subscription/receive-and-handle-callbacks)
- [多维表格概述](https://open.feishu.cn/document/server-docs/docs/bitable-v1/bitable-overview)
- [订阅云文档事件](https://open.feishu.cn/document/server-docs/docs/drive-v1/event/subscribe)
