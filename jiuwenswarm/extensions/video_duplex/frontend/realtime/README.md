# 实时语音模块

本模块随 `video_duplex` 插件部署，提供公共浏览器实时语音运行时和 Qwen/OpenAI 适配器。Swarm 和 TaskService 继续管理后台任务，媒体连接仅负责用户对话与任务播报。

## 分层与调用链

- `session.ts`：采集、重采样、播放、会话生命周期、用户轮次、打断和任务结果播报队列。通过 callbacks 和可注入 VAD 使用宿主能力。
- `audio/`：共享 PCM 转换、采集与播放 AudioWorklet。播放量按 response 统计；清空确认带序号，旧确认不会清空新一轮状态。
- `providers/contract.ts`、`registry.ts`：统一事件和适配器注册。新增厂商实现接口并注册；测试可直接注入 `adapter`。
- `providers/qwen.ts`：保留 16 kHz 输入、24 kHz 输出、本地 Silero 检测、Qwen 轮次及图像发送顺序。
- `providers/openai.ts`：GA Realtime 协议，24 kHz PCM、服务端 VAD、音频/文本/工具事件、稀疏图像输入。默认模型 `gpt-realtime-2.1-mini`，可配置。
- `taskBridge.ts`、`taskPrompts.ts`：任务调用解析和播报内容；公共运行时不直接调用 TaskService、Swarm 或 UI store。
- `index.ts`：浏览器宿主入口，解析网关地址并注入 Silero。JoyAI 保持原有独立链路。

UI 先经已有控制连接调用 `video.realtime.session`，网关验证用户和对话后创建配置快照，返回本地媒体地址和单次连接票据。`/ws/video/realtime` 消费票据，使用服务器保存的凭据连接模型。客户端看不到上游密钥。工具通过 `video.realtime.tool` 携带媒体会话 ID，后端再次验证所有权后进入现有 TaskService。

同一会话的工具调用按 call ID 幂等，不同媒体会话使用不同命令命名空间。断开媒体连接后拒绝新工具调用，但已提交任务继续运行，可通过已有任务列表和历史恢复。修改 provider、模型、音色或密钥只影响新会话。

## 打断与历史

OpenAI 服务端 VAD 创建响应并触发打断。客户端清空播放后，以 AudioWorklet 实际消耗的样本量发送 `conversation.item.truncate`，等待截断确认后再调度任务播报。迟到的音频不会恢复播放，首次迟到的未听片段会截断到零。取消生成、生成完成和播放完成分别处理。

生成文本作为生成记录保留，`chat.voice_playback` 另外记录 response ID、播放毫秒和是否打断；不按播放时长猜测已听到哪些字。不自动重连或重放媒体/工具调用；网络背压、播放溢出或同步超时关闭媒体会话。

## 配置

在设置 → 实验性功能的全双工模型配置中选择 Qwen 或 OpenAI。也可在服务器环境配置：

```dotenv
VIDEO_LIVE_MODE=realtime
VIDEO_REALTIME_PROVIDER=openai
OPENAI_REALTIME_URL=wss://api.openai.com/v1/realtime
OPENAI_REALTIME_API_KEY=your-server-side-key
OPENAI_REALTIME_MODEL=gpt-realtime-2.1-mini
OPENAI_REALTIME_VOICE=marin
```

密钥可回退到 `OPENAI_API_KEY`。Qwen 原有 `QWEN_OMNI_*` 配置继续使用；旧环境仅设置 `VIDEO_LIVE_MODE=realtime` 时默认 Qwen。设置读取只返回密钥配置状态，空密钥输入保留已有值。

OpenAI 接收采样图像，不是原生连续视频：最多每秒一帧，当前帧替换旧帧；关闭视频源删除旧图像。图片大小上限 256 KiB（base64 字符长度）。

## 验证

从仓库根目录运行：

```sh
python -m pytest jiuwenswarm/extensions/video_duplex/tests/backend --no-cov -o log_cli=false -q
cd jiuwenswarm/channels/web/frontend
npm run typecheck:realtime
npm run test:qwen-barge-in
npm run test:realtime-providers
npm run test:task-full-duplex
npm run test:joyai-workflow
npm run build
npm run test:realtime-worklet-build
```

自动化覆盖协议格式、采样率、工具幂等、会话归属、配置快照、迟到事件、播放计量和任务生命周期。上线前还需使用有模型权限的账号完成真实设备联调：连续说话、插话、任务执行期间断开/重开媒体、摄像头切换、长会话及网络中断。自动化通过不代表已验证账号权限、真实延迟或回声效果。

会话票据存放在网关进程内，多 worker 部署需连接路由保持同一进程，或后续引入共享会话存储。旧 Qwen RPC/WS 兼容入口保留；新 UI 使用绑定会话入口。本次不新增自动恢复策略，也不改造 Swarm 团队装配或任务持久化引擎。

协议依据：[OpenAI Realtime conversations](https://developers.openai.com/api/docs/guides/realtime-conversations)、[模型说明](https://developers.openai.com/api/docs/models/gpt-realtime-2.1-mini)、[Qwen 客户端事件](https://www.alibabacloud.com/help/en/model-studio/client-events)。
