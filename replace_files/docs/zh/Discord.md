# Discord 频道接入说明

本文说明如何在 [Discord 开发者平台](https://discord.com/developers/applications) 创建应用与 Bot、开启所需 Intent 与安装方式，并在 JiuwenClaw 的**频道管理**中完成配置。

## 本仓库中的实现

- Python 频道：`jiuwenclaw/channel/discord_channel.py`（基于 discord.py）
- 运行时配置：`config.yaml` 中的 `channels/discord`，或通过 Web 端 **频道管理** → Discord 保存

Bot 使用 **Bot Token** 登录，可在配置的服务器频道与/或私信（DM）中收发消息（也可通过 `block_dm` 关闭私信）。处理过程中会对用户消息尝试添加 👀 反应，与其它频道行为一致。

## 前置条件

- 已注册 Discord 账号
- 具备将 Bot 拉入服务器的权限；若启用用户安装，用户也可通过安装链接使用私信（取决于你在后台的安装设置）

---

## 1. 创建应用

1. 打开 [https://discord.com/developers/applications](https://discord.com/developers/applications)。
2. 点击 **New Application**，填写名称并创建。

![创建 Discord 应用](../assets/images/discord/1_create_new_app.png)

后续 **OAuth2 / 安装** 与 **Bot** 用户均在此应用下配置。

---

## 2. 获取 Bot Token（重置并复制）

1. 左侧进入 **Bot**。
2. 在 **Token** 区域点击 **Reset Token** 并确认。
3. **立即复制 Token** 并妥善保存；重置后 Discord 通常只完整展示一次。

![重置并复制 Bot Token](../assets/images/discord/2_reset_bot_token.png)

**安全提示**

- Token 等同于密码，泄露后他人可操控你的 Bot。
- 仅填入 JiuwenClaw 的 Discord 配置或 `config.yaml`，勿提交到代码仓。
- 若怀疑泄露，在同一位置再次 **Reset Token**。

该值对应 JiuwenClaw 中的 **`bot_token`**。

---

## 3. 开启 Message Content Intent

JiuwenClaw 需要读取用户消息的**文本内容**。Discord 要求通过「特权网关 Intent」显式开启。

1. 仍在 **Bot** 页面。
2. 在 **Privileged Gateway Intents** 中打开 **MESSAGE CONTENT INTENT**。
3. 若页面有保存提示，请保存。

![开启 Message Content Intent](../assets/images/discord/3_set_required_intent.png)

未开启时，Bot 可能在线但无法读取普通消息的文本内容。

---

## 4. 服务器安装：权限范围与 Bot 权限

配置 Bot 被安装进服务器后的能力。JiuwenClaw 典型需要：

- 在目标频道**读取**消息
- **发送**消息（回复用户）
- **读取消息历史**（上下文）
- **添加反应**（例如处理中的 👀）
- **使用应用程序命令**（若你另行使用斜杠命令，可按需勾选）

在开发者后台的 **Installation** 或 **OAuth2 → URL Generator** 等入口（界面可能随 Discord 更新调整）中：

- 勾选 **`bot`** 等所需 **Scopes**（若使用应用命令可勾选 **`applications.commands`**）。
- 勾选与上表一致的 **Bot Permissions**。
- 下方截图中显示了推荐的权限配置，必选内容已标出。

![服务器安装范围与 Bot 权限](../assets/images/discord/4_set_guild_install_scope_and_permissions.png)

请以截图与上述能力为准；若菜单名称与截图略有差异，选择等效项即可。

---

## 5. 安装方式与安装 / 设置链接

选择用户或管理员如何安装应用：

- **Guild install（服务器安装）** — 将 Bot 加入指定服务器，需要可分享的安装或邀请链接。
- **User install（用户安装）** — 允许用户为**私信（DM）**安装应用（适合主要走私聊、不固定某个频道）。

在门户中复制生成的 **URL** 或 **Install link**，在浏览器中打开完成授权。

![选择安装方式并复制设置链接](../assets/images/discord/5_select_install_methods_and_copy_setup_link.png)

安装完成后：

- **服务器场景**：将 Bot 放入 JiuwenClaw 要监听的频道（见下文 `guild_id` / `channel_id`）。
- **私信场景**：用户可与 Bot 打开私聊（除非在 JiuwenClaw 中开启 **`block_dm`**）。

---

## 6. JiuwenClaw 需要的 ID

| 配置项 | 获取方式 |
|--------|----------|
| **Application ID** | **General Information** → **APPLICATION ID**，复制。对应 **`application_id`**（建议填写）。 |
| **Guild ID（服务器 ID）** | Discord 用户设置中开启 **开发者模式**（高级）。**右键服务器图标** → **复制服务器 ID**。对应 **`guild_id`**。若仅私信、不限制服务器可留空。 |
| **Channel ID（频道 ID）** | **右键文字频道** → **复制频道 ID**。对应 **`channel_id`**。若仅私信等场景可按需留空。 |

当 **`guild_id`** 与 **`channel_id`** 均填写时，只处理**该服务器下该频道**内的消息；**私信仍可收发**，除非将 **`block_dm`** 设为 `true`。

有一个快速取得 `guild_id` 与 `channel_id` 的小技巧。在 Discord 网页版中打开目标文字频道，并查看浏览器地址栏，URL 格式一般为：
```
https://discord.com/channels/<guild_id>/<channel_id>
```

---

## 7. 在 JiuwenClaw 频道管理中配置

在 Web 端打开 **频道管理** → **Discord** 填写并保存，或直接编辑：

`~/.jiuwenclaw/config/config.yaml`（路径因环境可能不同）

示例：

```yaml
channels:
  discord:
    bot_token: "你的Bot_Token"
    application_id: "你的Application_ID"
    guild_id: "你的服务器ID"      # 仅私信场景可留空
    channel_id: "你的频道ID"      # 按需留空
    block_dm: false              # true 表示不处理私信
    allow_from: []               # 空为不限制；否则填 Discord 用户 ID 列表
    enabled: true
```

| 字段 | 说明 |
|------|------|
| `bot_token` | **必填**，来自第 2 步。 |
| `application_id` | **General Information** 中的应用 ID。 |
| `guild_id` | 限定服务器；仅 DM 时可不填。 |
| `channel_id` | 限定频道；可与 DM 并存时按需填写。 |
| `block_dm` | `true` 时忽略所有私信。 |
| `allow_from` | 用户白名单；空表示不限制（仍需能向 Bot 发消息）。 |
| `enabled` | 是否启用 Discord 频道。 |

---

## 8. 验证

1. 确认 Bot 在服务器中在线，或用户侧可与 Bot 私信。
2. 在已配置频道或私信中发送一条短消息。
3. 若 Bot 在该场景下有 **添加反应** 权限，用户消息上应出现 👀；模型与下游配置正确时，应收到智能体回复。

---

## 故障排查

### Bot 在线但没有回复

- 确认已开启 **MESSAGE CONTENT INTENT**。
- 确认 **`enabled: true`** 且 **`bot_token`** 正确。
- 检查 **`guild_id` / `channel_id`**：二者都填时，仅监听该频道内消息。
- 若配置了 **`allow_from`**，当前用户的 Discord 用户 ID 须在列表中（或改为空列表）。

### 无法添加 👀 反应

- 在该频道为 Bot 所在角色勾选 **添加反应（Add Reactions）** 权限（或通过频道权限覆盖）。

### 私信不可用

- 若需用户私信使用，需要在Discord进行 **User install**。
- JiuwenClaw 中确认 **`block_dm`** 为 `false`。
- 用户安装后通常需在 Bot 资料页选择 **发消息** 打开 DM。

### 模型或下游 API 报错

Discord 连通与模型配置相互独立。若出现模型 HTTP 错误，请检查 `.env` 与模型相关配置并重启应用（与其它频道文档说明一致）。

---

## 相关文档

- 简要字段说明：[频道.md](频道.md) 中的 Discord 小节
