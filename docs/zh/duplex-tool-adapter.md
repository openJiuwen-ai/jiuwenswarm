# vLLM-Omni 双工工具调用

视频双工界面可连接 vLLM-Omni Realtime WebSocket 后端。需要包含 [PR #9909](https://github.com/vllm-project/vllm-omni/pull/9909) 工具能力的已验证部署版本。本接入不支持 HTTP 工具调用或进程内推理 API。

在“设置 → 实验性功能”填写 vLLM-Omni WebSocket 地址、部署模型标识、可选 API 密钥及工具超时，选择 **vLLM-Omni WebSocket** 后重新打开双工会话。已有会话保持原连接。原有双工连接测试会检查当前保存的后端和工具能力。

受管部署也可使用以下环境配置：

```sh
VIDEO_LIVE_MODE=realtime
VIDEO_REALTIME_PROVIDER=vllm_omni_ws
VLLM_OMNI_REALTIME_URL=ws://127.0.0.1:8099/v1/realtime
VLLM_OMNI_MODEL_NAME=<部署模型标识>
VLLM_OMNI_TOOL_TIMEOUT_S=120
# 仅后端需要认证时设置 VLLM_OMNI_API_KEY。
```

后端须声明 declared 工具能力，并支持 client-response-create 或 automatic 结果续答。无工具、delegate-only、next-observation 后端在本接入中会被拒绝。首期开放九问任务委托和队列管理，任务仍由 Core 按既有权限和审批卡片执行；语音或模型文本不能通过此适配器回答审批卡片。

工具执行时音视频继续流动，审批等待也计入超时。最多同时执行一个工具，失败、取消和迟到结果不会作为成功输出回传。停止或断开会取消本连接拥有的任务，但无法撤销已经提交的外部副作用。不会自动重连或重放执行；连接失败后请重新打开双工，并先查看任务面板再决定是否重试。

凭据只保留在服务端，浏览器临时连接票据不得分享。原 Qwen 云端和 JoyAI 接入保持原路径。
