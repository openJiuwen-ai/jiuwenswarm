# 全双工视频插件

`video-duplex` 是 JiuwenSwarm 的全栈 Application Plugin。摄像头、共享屏幕、
麦克风、模型协议、ASR/TTS 和搜索编排均位于本插件目录；Jiuwen 核心只提供通用的
插件发现、页面挂载、RPC/WebSocket 路由和 Core Agent 服务注入。

```mermaid
flowchart LR
    Browser[浏览器媒体与对话] --> Host[Application Plugin Host]
    Host --> Plugin[extensions/video_duplex]
    Plugin --> JoyAI[JoyAI Chat Completions]
    Plugin --> Qwen[Qwen Omni Realtime]
    Plugin --> Voice[ASR / TTS]
    Plugin --> Core[Jiuwen Core Agent 搜索]
```

## 启用与配置

优先在 Jiuwen 侧栏打开 **扩展 → 应用插件 → Full-duplex → 设置**。配置表单、字段显示、
密钥占位和保存逻辑均由 `video-duplex` 插件提供；核心前端只负责发现并挂载该组件。
禁用插件后，全双工功能入口会隐藏，但 **扩展 → 应用插件** 中的设置入口仍保留，可用于重新启用。

实例配置文件：

- Windows：`%USERPROFILE%\.jiuwenswarm\config\.env`
- macOS/Linux：`~/.jiuwenswarm/config/.env`

在设置页保存后，新请求会直接使用最新配置。手动修改 `.env` 时需要重启 Gateway。
`VIDEO_DUPLEX_ENABLED=false` 会禁用插件并隐藏全双工侧栏入口。

### JoyAI + OpenAI 兼容 ASR/TTS

```dotenv
VIDEO_DUPLEX_ENABLED=true
VIDEO_LIVE_MODE=joyai
JOYAI_API_BASE=https://modelservice.jdcloud.com/v1
JOYAI_API_KEY=pk-your-key
JOYAI_MODEL_NAME=jdopensource/JoyAI-VL-Interaction

VOICE_PROTOCOL=openai_http
VOICE_ASR_ENDPOINT=https://api.siliconflow.cn/v1/audio/transcriptions
VOICE_TTS_ENDPOINT=https://api.siliconflow.cn/v1/audio/speech
VOICE_API_KEY=sk-your-key
VOICE_ASR_MODEL=FunAudioLLM/SenseVoiceSmall
VOICE_TTS_MODEL=FunAudioLLM/CosyVoice2-0.5B
VOICE_TTS_VOICE=FunAudioLLM/CosyVoice2-0.5B:anna
```

`JOYAI_API_BASE` 填到 `/v1`，不要填到 `/chat/completions`。JoyAI 视觉模型不直接
处理麦克风或生成语音，因此必须配置独立 ASR/TTS。

如使用 JoyAI 原生语音 WebSocket：

```dotenv
VOICE_PROTOCOL=native_ws
VOICE_ASR_ENDPOINT=ws://127.0.0.1:8994/ws/asr
VOICE_TTS_ENDPOINT=ws://127.0.0.1:8992/ws/tts
```

### Qwen Omni Realtime

```dotenv
VIDEO_DUPLEX_ENABLED=true
VIDEO_LIVE_MODE=realtime
VIDEO_REALTIME_PROVIDER=qwen_omni
QWEN_OMNI_REALTIME_URL=wss://your-workspace.example.com/api-ws/v1/realtime
QWEN_OMNI_API_KEY=sk-your-key
QWEN_OMNI_MODEL_NAME=qwen3.5-omni-flash-realtime
QWEN_OMNI_VOICE=Cherry
```

浏览器连接 Jiuwen Gateway 的 `/ws/video/qwen-omni`，上游地址和密钥只留在服务端。

## 运行流程

```mermaid
sequenceDiagram
    participant U as 用户
    participant P as video-duplex 前端
    participant B as video-duplex 后端
    participant M as 多模态模型
    participant C as Core Agent
    U->>P: 画面、语音或文字
    P->>B: 插件 RPC / WebSocket
    B->>M: 当前媒体与指令
    M-->>B: 回答或搜索意图
    opt 需要搜索
        B->>C: 标准 chat.send
        C-->>B: 进度与检索结论
        B->>M: 工具结果
    end
    M-->>P: 文字与语音
```

| 能力 | JoyAI | Qwen Omni Realtime |
|---|---|---|
| 模型连接 | 逐帧 Chat Completions | 持久 WebSocket |
| 语音 | 独立 ASR/TTS | 模型原生音频 |
| 搜索触发 | 模型 delegation | function call |
| 搜索执行 | Jiuwen Core Agent | Jiuwen Core Agent |
| 打断 | 停止独立 TTS | `response.cancel` |

## 使用与验证

1. 启动 Jiuwen，侧栏进入 **Full-duplex**。
2. 选择摄像头、共享屏幕或本地视频并授予权限。
3. 用语音或文字提问，确认文字、语音和打断正常。
4. 对需要外部信息的问题，确认搜索进度和最终回答均返回。
5. 在 **扩展 → 应用插件** 中禁用，确认全双工入口隐藏，但管理页仍可重新启用。

日志位于 `~/.jiuwenswarm/logs/`：

| 文件 | 内容 |
|---|---|
| `joyai-video.jsonl` | 帧请求、原始结果、延迟和限流 |
| `asr-results.jsonl` | 转写、空结果、噪音过滤和延迟 |
| `video-task-routing.jsonl` | 搜索、TTS 和工具调用 |
| `realtime-interrupt.jsonl` | Realtime 状态与打断 |

## 代码入口

| 职责 | 文件 |
|---|---|
| 插件注册与贡献 | `extension.py`、`extension.yaml` |
| 插件设置页面 | `frontend/VideoDuplexSettings.tsx` |
| 插件设置持久化 | `backend/settings.py` |
| 页面 | `frontend/VideoLivePanel/index.tsx` |
| JoyAI 调度 | `frontend/VideoLivePanel/joyaiProvider.ts` |
| Qwen 会话 | `frontend/VideoLivePanel/qwenOmniSession.ts` |
| 后端编排 | `backend/video_live.py` |
| ASR/TTS | `backend/video_voice.py` |
| 搜索 | `backend/video_search.py` |
