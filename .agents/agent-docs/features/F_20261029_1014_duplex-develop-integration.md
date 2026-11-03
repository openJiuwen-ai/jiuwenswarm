---
id: F_20261029_1014_duplex-develop-integration
title: 全双工修复移植到 develop
type: F
author: yzdnh123
author_email: yzdnh123@gmail.com
modules:
  - jiuwenswarm/extensions/video_duplex
  - jiuwenswarm/server/runtime/video_task
  - gateway/extensions/video_duplex
files:
  - jiuwenswarm/server/runtime/video_task/execution.py
  - jiuwenswarm/server/runtime/video_task/server_adapter.py
  - jiuwenswarm/extensions/video_duplex/backend/video_search.py
  - jiuwenswarm/extensions/video_duplex/tests/backend/test_video_live.py
  - jiuwenswarm/extensions/video_duplex/tests/backend/test_task_delivery_failures.py
designs: []
design_note: 用户明确要求将已有修复移植至官方 develop，本次没有新增独立设计。
---

# 全双工修复移植到 develop

2026-10-29，目标 PR：[10013](https://github.com/openJiuwen-ai/jiuwenswarm/pull/10013)。
比较基线为官方 develop 的 `8407baaee`，原工作区保存提交为 `41236646d`。
原分支混有大量 0.2.8 历史，直接作为 PR 来源会夹带无关变更。因此在 develop 上逐项移植用户和 zuiho-kai 的相关修复，保留提交作者及来源。完整提交清单以 PR 描述为准。

## 适配决策

沿用 develop 的进程边界及移动后的模块，不恢复旧目录：

```mermaid
flowchart LR
    UI[全双工前端] --> GW[Gateway 任务端点]
    GW -->|E2A 请求| AS[AgentServer video_task]
    AS --> Core[会话 Agent 与执行任务]
    Core -->|检查点与终态| GW
    GW -->|队列与回执| UI
```

- Gateway 端使用根目录 `gateway/`；执行绑定、回调和服务端适配使用 `server/runtime/video_task/`。
- 通过既有协议兼容入口补齐 receipts 与 pending_interrupt 方法，保留 develop 的枚举组织。
- 媒体入口使用 Gateway 的异步 normalizer。没有上传传输时保留原始图像交给 AgentServer，不要求 Gateway 本地落盘。
- 恢复确认卡片拦截：从磁盘恢复的待处理卡片不能把新委派任务当作卡片答案。已有回归验证在执行绑定前拒绝任务。
- 历史持久化屏障测试单独隔离 lifecycle admission，防止人为暂停的写入锁阻止测试进入其目标屏障；真实文件写入和最终附件读取仍参与测试。
- 前端保留 develop 的模型选择与 Design 停止控件，补齐共享状态、历史事件重试和子代理事件消费者。

本次为版本移植，不替代原修复的产品决策。源版本中英文使用说明随对应修复保留。

## 验证证据

候选为 `c5d50166a` 加本次执行拦截与测试适配。使用独立 worktree 环境，安装 develop 锁定的 Core `b2a0a248`、protocol `10a03dd2` 和 Windows 文件锁依赖 pywin32，原工作区依赖未改动。

| 范围 | 命令 | 结果 |
| --- | --- | --- |
| 全双工后端 | `python -m pytest jiuwenswarm/extensions/video_duplex/tests/backend --no-cov -q --tb=short` | 639 passed，72.96 秒 |
| Qwen 前端 | `npm run test:qwen-barge-in` | 133 passed |
| 全双工生命周期和 Design | `npm run test:task-full-duplex` | 62 passed |
| 图像编码 | `npm run test:video-frame-encoding` | 10 passed |
| 设置校验 | `node --test tests/videoDuplexSettings.test.mjs` | 3 passed |
| 子代理事件 | `npm run test:duplex-subagent-panel` | 3 passed |
| Electron 共享选择器 | `node --test tests/electron/display_media_picker.test.cjs` | 4 passed |
| 前端生产构建 | `npx vite build` | 通过 |

后端覆盖任务数据库状态、取消结算、队列操作、回执、工具委派、错误诊断和附件交付。前端验证发生在上述候选之前，之后仅改动后端执行绑定和后端测试，前端构建产物未受影响。未运行格式化或静态检查。

未完成真实供应商端到端测试及 macOS/Electron 实机联调，PR 保持草稿；这些限制不等同于自动化用例失败。后续修改协议、执行装配或媒体接口时须重跑对应回归。


## 2026-10-29 权限卡语音输入完善与 develop 合并

按用户要求，提取本地 `duplex-local-5570-5597-028` 工作树中权限卡相关未提交分块，未带入同一工作树的模型认证及错误提示等其他修改。
工具权限申请 `permission_interrupt` 只接受界面点击，包括允许、拒绝及记住权限。执行确认 `confirm_interrupt` 保留语音回答，新增“按计划执行”等明确表达与实际选项映射。前端清理同任务的旧确认状态；服务端拒绝权限卡语音答案，语音队列摘要不暴露可提交的权限卡选项。手动点击路径保持可用，中英文说明同步。

合入官方 develop `b1301d04a`。协议文件冲突保留官方向 `openjiuwen_protocol.message` 的类型迁移，只额外注册全双工 receipts 和 pending_interrupt 方法。测试环境协议依赖同步到官方锁定的 `2a0457e5`。

合并候选验证：全双工后端、协议契约、Gateway 图片归一化及中断状态回归共 654 passed（87.34 秒）；Qwen 前端 136 passed，全双工生命周期 62 passed，JoyAI 工作流 27 passed。新增用例验证权限卡语音拒绝后仍能手动完成、旧卡状态不会串用、执行确认使用真实选项。真实供应商和跨平台实机测试未执行。


## 2026-11-03 再次同步 develop 并解决 PR 冲突

合入官方 develop `b825ac938`，在现有 PR 提交上普通合并，不新建分支、不改写历史。

- 续接请求合并官方 `interrupt_id` 与全双工 execution_target，保留 Design 模式及项目定位；回归同时检查中断标识、卡片标识和执行目标。
- Runtime 沿用官方租户 scoped_manager 与 interaction_ids 协议；全双工回执清理在该作用域的 finally 内执行。
- ChatPanel 保留企业版心跳入口限制，全双工仍由 App 的 ApplicationConversationRuntimes 承载，删除局部重复运行组件与无调用的旧回调。
- Electron 打包同时保留官方 cdp_port 与全双工 display_media_picker；不恢复官方已删除的 update_shutdown。
- 修复自动合并后的语义冲突：permission_continuation 清理函数引用了重构前的局部 state_signature，异常被捕获后导致旧中断状态恢复。改为从当前已校验的 state.ai_message.tool_calls 获取名称，并验证真实 Core 中断清理、取消及语音任务不消费权限卡的行为。
- 测试使用迁移后的 server/runtime/video_task 路径；前端夹具同步浏览器传输初始化、路由负责选择 Design 会话及历史写入屏障。

测试环境使用独立 worktree 依赖：Core `c8c621b2`、protocol `5201ca86`；前端按官方 package-lock 安装独立 node_modules，未改动 Desktop 主工作区依赖。官方 uv.lock 保持原样。
前端 Qwen 136、全双工生命周期 62、JoyAI 27、Design 入口 20、Electron 共享选择器 4 项通过，共 249 项；Vite 生产构建通过（1m07s，仅有 chunk 体积警告）。
后端最终回归 1145 passed（162.34 秒），包括全双工完整后端、协议与 Gateway、Runtime 及租户作用域、交互续接、真实 Core 权限卡取消/恢复、桌面打包契约。后端与前端本轮共 1394 项自动化测试通过。未执行真实供应商及跨平台实机音视频测试。


## 2026-11-03 审批卡片清理检视修复

核对 PR 评论 `6595376062`（候选 `673e95d8c`）。`question:${jobId}:` 在空 jobId 时实际为 `question::`，不会匹配非空任务 ID，但会匹配全部聊天卡片；任意聊天卡片结束都会误清理其余聊天卡片，Qwen 尚未播报的问题队列也有相同清理范围问题。

Qwen 与 JoyAI 对聊天卡片按 `interaction.id || request_id` 精确清理，缺少标识时不清理其他卡片；非空任务通过保存的 job_id 精确匹配清理，避免任务 ID 的冒号前缀碰撞。Qwen 的权限卡替换与待播报问题队列使用相同聊天卡片边界。任务结束仍可清理该任务的全部确认卡，其他任务及聊天卡保留。权限申请只允许手动点击的规则不变。

本轮回归通过：Qwen 138、JoyAI 27、全双工入口及生命周期 62，共 227 项。新增用例检查关闭一张聊天卡后，其他聊天卡及托管任务卡仍在下一轮模型提示中；检查 Qwen 排队问题、权限卡替换、未知关闭事件及含冒号的不同任务 ID。Vite 生产构建通过（1m13s，第三方注释和 chunk 体积提示）。本轮只改前端卡片生命周期，未重跑后端或真实供应商音视频联调。
