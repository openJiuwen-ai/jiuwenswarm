# WhatsApp 频道接入说明

本文档说明当前仓库里已经实现的 WhatsApp 接入方式。

## 架构

JiuwenClaw 的 Python 代码不会直接连接 WhatsApp。

`WhatsApp 客户端` <-> `Baileys bridge（Node.js）` <-> `WhatsAppChannel（Python）`

- Bridge WebSocket 默认地址：`ws://127.0.0.1:19600/ws`
- Bridge 脚本：`jiuwenclaw/scripts/whatsapp-bridge.js`
- Python 频道实现：`jiuwenclaw/channel/whatsapp_channel.py`
- 运行时配置位置：`config.yaml` 里的 `channels.whatsapp`

## 当前仓库已经实现的能力

- Node bridge 使用 Baileys 登录 WhatsApp Web，并负责收发消息。
- Python 侧通过 WebSocket 连接本地 bridge，交换 JSON 消息。
- Python 侧会根据 bridge 发来的状态事件维护连接状态，只有在 WhatsApp 真正连通时才允许发送消息。
- `allow_from` 可以按完整 JID 或手机号部分过滤入站发送者。

## 当前仓库还没有实现的能力

- 没有 Python 到 bridge 的鉴权握手，也没有共享密钥 token。
- 目前默认仅依赖 bridge 绑定到 `127.0.0.1` 来限制访问范围。
- 没有实现媒体文件下载和附件转发。
- 没有实现语音消息转写。
- 没有实现 Python 侧入站消息去重。

## WebSocket 协议

Python 发给 bridge 的消息：

```json
{
  "type": "send",
  "jid": "123456789@s.whatsapp.net",
  "text": "hello",
  "request_id": "msg-123"
}
```

Bridge 发给 Python 的消息类型：

- `status`：bridge / WhatsApp 连接状态更新
- `qr`：当前有二维码可供扫码登录
- `inbound`：来自 WhatsApp 的入站文本消息
- `send_result`：对 `send` 的确认或错误返回
- `pong`：对 ping 的响应

## 连接状态语义

Python 侧当前会追踪这些状态：

- `stopped`：频道已停止
- `bridge_connected`：Python 已连上本地 bridge WebSocket，但 WhatsApp 可能仍在连接中
- `connecting`：bridge 正在尝试把 Baileys 连到 WhatsApp
- `qr_pending`：已经生成二维码，等待扫码
- `open`：WhatsApp 已连接，可正常发送消息
- `close`：WhatsApp 连接已关闭
- `logged_out`：WhatsApp 会话已登出，需要重新扫码
- `bridge_disconnected`：Python 到本地 bridge 的 WebSocket 断开

这几个状态需要区分清楚：

- `bridge_connected` 只表示 Python 能连到本地 bridge。
- `open` 才表示 bridge 已经真正登录到 WhatsApp，可以发消息。

## 频道元数据

`WhatsAppChannel.get_metadata()` 现在会在 `extra` 中暴露这些运行态字段：

- `bridge_state`
- `bridge_ws_connected`
- `whatsapp_connected`
- `qr_pending`
- `last_status_ts_ms`
- `last_status_code`

## 前置要求

- 可运行 `python -m jiuwenclaw.app` 的 Python 环境
- Node.js 20+ 和 npm
- 已开通 Linked Devices 的 WhatsApp 账号

## 1. 安装 Bridge 依赖

在内层项目目录执行，也就是包含 `jiuwenclaw/package.json` 的目录：

```powershell
cd C:\Users\chiak\OneDrive\Desktop\jiuwenclaw\jiuwenclaw
npm install
```

如果只想安装 bridge 依赖：

```powershell
npm install @whiskeysockets/baileys ws pino qrcode-terminal
```

## 2. 配置 WhatsApp

编辑运行时配置文件：

`C:\Users\chiak\.jiuwenclaw\config\config.yaml`

在 `channels:` 下添加或确认如下配置：

```yaml
  whatsapp:
    bridge_ws_url: ws://127.0.0.1:19600/ws
    default_jid:
    allow_from: []
    enable_streaming: true
    auto_start_bridge: false
    bridge_command: node scripts/whatsapp-bridge.js
    bridge_workdir: C:/Users/chiak/OneDrive/Desktop/jiuwenclaw/jiuwenclaw
    enabled: true
```

说明：

- `enable_streaming: true` 会转发中间事件，例如 token 流式输出和工具执行进度。
- `enable_streaming: false` 会抑制 `chat.delta`，更接近只发最终结果。
- `default_jid` 是出站消息缺少目标时的兜底发送对象。
- `allow_from` 支持完整发送者 JID，也支持 `@` 前面的号码部分。
- `auto_start_bridge: true` 表示由 Python 自动拉起 Node bridge。

## 3. 启动服务

如果没有开启 `auto_start_bridge: true`，请开两个终端。

终端 A，启动 bridge：

```powershell
cd C:\Users\chiak\OneDrive\Desktop\jiuwenclaw\jiuwenclaw
npm run whatsapp:bridge
```

预期日志：

`[whatsapp-bridge] ws://127.0.0.1:19600/ws`

终端 B，启动应用：

```powershell
cd C:\Users\chiak\OneDrive\Desktop\jiuwenclaw
python -m jiuwenclaw.app
```

预期行为：

- 应用日志会显示 `WhatsAppChannel` 已注册。
- 频道先进入 `bridge_connected`。
- 后续根据登录状态进入 `connecting`、`qr_pending` 或 `open`。

## 4. 扫码绑定 WhatsApp

如果 bridge 没有可用的认证状态，它会在 bridge 终端打印二维码。

在 WhatsApp 中：

`设置` -> `已关联设备` -> `关联设备`

扫描终端 A 中的二维码即可。

认证状态会保存在：

`jiuwenclaw/jiuwenclaw/workspace/.whatsapp-auth`

如果账号已经绑定，可能不会再次出现二维码，这是正常现象。

## 5. 收发消息说明

- 入站文本消息会由 bridge 以 `inbound` 发给 Python。
- 出站消息由 Python 以 `send` 发给 bridge。
- Python 现在只会在状态为 `open` 时尝试发送。
- 如果 bridge 返回 `send_result.ok=false`，Python 会记录失败日志。

## 6. 安全说明

- bridge 默认只监听 `127.0.0.1`，因此默认只允许本机访问。
- 当前仓库还没有实现 bridge token 鉴权。
- 如果你要把 bridge 端口暴露到其他机器，请先补上鉴权，或者放到可信的隧道/反向代理之后。

## 7. LLM 配置仍然会影响最终效果

如果 WhatsApp 入站消息进入应用后出现 `405 Not Allowed` 之类的下游错误，通常不是 WhatsApp 本身的问题，而是模型配置仍然是占位值或无效。

示例 `.env`：

```env
MODEL_PROVIDER=OpenAI
MODEL_NAME=your-real-model
API_BASE=https://your-real-openai-compatible-endpoint/v1
API_KEY=your-real-key
```

更新 `.env` 后重启 `python -m jiuwenclaw.app`。

## 故障排查

### `Missing script: "whatsapp:bridge"`

说明你在错误目录执行了 `npm`。请使用：

`C:\Users\chiak\OneDrive\Desktop\jiuwenclaw\jiuwenclaw`

### Bridge 启动了但没有二维码

1. 先停止旧的 bridge 进程，避免端口或旧会话冲突。
2. 删除认证目录，强制重新绑定：
   `jiuwenclaw/jiuwenclaw/workspace/.whatsapp-auth`
3. 重新启动 bridge，等待出现 `QR received`。

### App 能连上 bridge，但发不出消息

先看日志里的状态：

- 如果是 `bridge_connected` 或 `connecting`，说明本地 bridge 可达，但 WhatsApp 还没准备好。
- 如果是 `qr_pending`，先扫码。
- 如果是 `logged_out`，删除认证目录后重新扫码。
- 只有 `open` 才允许真正发送。

### App 提示 WhatsApp 未配置

- 确认 YAML 路径是 `channels.whatsapp`
- 确认设置了 `enabled: true`
- 确认 `bridge_ws_url` 不为空

### 模拟器扫码问题

和模拟器相机透传相比，直接用实体手机扫码通常更稳定。
