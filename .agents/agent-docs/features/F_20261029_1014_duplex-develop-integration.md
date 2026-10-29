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
