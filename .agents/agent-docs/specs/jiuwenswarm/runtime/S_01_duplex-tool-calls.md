---
id: S_01_duplex-tool-calls
title: 双工工具调用与后端接纳规约
type: S
author: chickeyton
author_email: ngton2014@gmail.com
modules: [jiuwenswarm/runtime, jiuwenswarm/extensions/video_duplex]
files:
  - jiuwenswarm/runtime/tool_calls/types.py
  - jiuwenswarm/runtime/tool_calls/coordinator.py
  - jiuwenswarm/extensions/video_duplex/backend/tool_adapters/vllm_omni.py
  - jiuwenswarm/extensions/video_duplex/backend/duplex_session_bridge.py
created: 2026-11-16
updated: 2026-11-16
---

# 双工工具调用与后端接纳规约

本契约用于 server-managed duplex WebSocket 路径。普通 core 模型工具循环及客户端工具中断路径保持原契约。

## 身份、授权与执行

`ToolCall` 保留 backend session、generation、call id、工具名及 JSON object 参数。传输恢复或新连接不能复用上一 generation 的执行资格。同一 call id 的两个完整事件投影最多执行一次，结束记录保留到会话关闭；收到 settled 后的迟到投影也不能执行。

业务 command id 为上述三元组的 SHA-256 命名空间，原始 call id 只用于后端关联。owner/scope 由配置 RPC 的已授权连接确定，通过短时一次性 ticket 绑定媒体 WebSocket；不得由模型参数覆盖。新路径不接受浏览器自行提交工具 output、修改工具目录或直接生成 response。

登录模型的凭据引用由原鉴权连接按实际执行目标解析、刷新，通过共享 submit_task 的 credential_ref 参数写入 TaskService 私有 request.login_credential_ref；模型参数不能提供或替换它，原始登录会话和令牌不进入任务记录或模型上下文。委托画面来自已验证的浏览器媒体，在工具调用开始时捕获，后续路由/认证等待期间的新帧不能改写该次委托画面。

首期只允许 `jiuwen_delegate` 和 `jiuwen_queue_manage`。后者必须 `allow_answers=False`，嵌套操作不能扩大回答权限。任务提交复用 `submit_task` 和现有 TaskService/AgentServer 链，不能旁路宿主权限和审批。工具最多一个并行执行，超额调用在产生业务副作用前拒绝。

保存会话在签发 ticket 时及每次工具执行前通过现有元数据 RPC 检查 owner 和 archived；归档会话不能新建调用。连接期间每秒检查元数据，检测归档、删除或 owner 变化后关闭连接及收尾。队列管理返回 `incomplete` 时不是工具成功，不把其中诊断当作成功 output；已提交的既有副作用保留其原生事实。

## 接纳与后续生成

发送 output 不等于后端接纳。提交时指定由调用身份派生的唯一 output item id；vLLM-Omni 回送 `conversation.item.added/created/done` 的已提交 function_call_output item，匹配该 item id、call id 及当前 generation 后才确认接纳。显式 created=false、相关错误或 settled 不得触发成功投递；内部 created=true 不属于标准 Realtime wire 必备字段。没有正向确认时不能生成成功 continuation。

标准 `response.function_call_arguments.done` 不带工具名，不能凭此猜测绑定；等候完整的 `response.output_item.done`。显式带工具名的完整扩展投影可以归一化，但必须按调用身份去重。

`client_response_create` 在接纳后最多调度一次 response.create；活跃 response 结束前保留待发送标记。存在音频输出时，还须等待 `playback.acknowledged` 的后端 cursor 确认 committed_ms 不小于 sent_ms，或后端清空对应播放。`response.done` 仅表示生成结束，不表示播放完成。`automatic` 不发送 response.create。成功接纳只封闭结果提交资格；其后续生成资格独立管理。显式取消、会话关闭或 generation 更换撤掉未发送 continuation。

## 生命周期与媒体

调用超时、后端 settled 或连接关闭先关闭本地结果出口，再收尾执行。失败工具原文不作为 function_call_output。取消只针对本次拥有的任务；既存任务管理操作不因此取消目标任务。取消已完成任务不得附带会被 TaskService 当作新任务处理的用户指令。原生执行未结算时保留 unknown/unsettled 事实，不重做副作用。

新路径无自动重连或调用重放。媒体保持独立收发；可靠事件队列满时显式关闭。支持 image item 的 turn 后端通过替换当前 `input_image` 消息接收画面，支持 video track 的后端使用裸 base64 video_frames；不按模型名称推断能力。音频使用明确的 PCM16/采样率，浏览器按输出采样率重采样后播放。

摄像头字段只接受有界的 JPEG/PNG/WebP base64 data URL，不能触发后端抓取远程图片 URL。明确文字提问也携带当前画面，不依赖先前语音输入。后端 `output_audio_buffer.cleared` 及已取消响应必须清空本地排队播放，迟到音频不得重新进入播放；旧响应终态不能释放新的响应。

配置键、使用步骤和限制见中英文 `duplex-tool-adapter.md`；验证事实见 `F_20261116_1142_duplex-tool-call-adapter`。
