---
id: B_20261107_1505_6226-forwarding-replay
title: 历史回放触发转发队列溢出及前端计时不收尾
type: B
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - jiuwenswarm/server/supervisor
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/server/supervisor/runtime_supervisor.py
  - jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts
  - tests/unit_tests/agentserver/test_supervisor_forwarding.py
  - jiuwenswarm/channels/web/frontend/tests/sessionInput.test.mjs
designs: []
design_note: 对现有有界转发及前端终态处理的小范围修复，无独立设计。
---

# 历史回放触发转发队列溢出及前端计时不收尾

关联 issue：#6226、#6229、#6230。基线为 releases/0.2.9（9756e9901）及 develop（70abc915d）；两个基线的相关代码一致。

## 现象与根因

现场历史表明定时任务已生成最终回复并记录成功，但集中回放期间 Supervisor 的 256 条转发队列溢出，前端收到 GATEWAY_FORWARD_FAILED 后仍在计时。切换会话后本地成功历史与实时错误展示不同，进度及错误标记也可能暂时不一致。

IPC 缓冲区已有数据时 recv 和处理消息不一定让出事件循环，独立转发协程可能尚未执行队列就满了。最小复现中 Gateway 完全不阻塞，突发 600 条消息仍失败；接收后主动让出调度即可按序完成。

前端普通 chat.error 只清思考态、写错误，依赖后续 processing=false 结束计时；转发终态失败时该帧可能不再到达。

## 修复与范围

- Supervisor 每次接收、处理 IPC 消息后主动让出调度；保留队列 256 条、16 MiB 字节限制、发送超时、失败处理与心跳路径。
- 仅对 is_complete=true 的 GATEWAY_FORWARD_FAILED 收尾前端 processing、streaming 及会话忙碌展示，不自动发送排队输入，不据此取消 Worker 执行。
- 按 session 记录最近 processing=true 的 request_id；已知属于旧请求的转发终态错误在解析执行身份前忽略，避免停止或覆盖新请求。
- 不改 Gateway、协议、历史持久化、任务规划或看板恢复机制。#6230 的 intro-finalize 取消后重建为 intro-finalize-v2 是真实工具操作，不按标题去重。

## 验证

Windows 项目 .venv，使用本地依赖与可控 WebSocket 替身，无真实模型调用。

- `.venv/Scripts/python.exe -m pytest tests/unit_tests/agentserver/test_supervisor_forwarding.py -m "not slow" -o addopts="" -q`：12 通过，1 个真实子进程慢测未执行。覆盖普通路由及 server-push 的 600 条缓存回放、另一请求正常完成、真正阻塞的超时/溢出、心跳和代际隔离。
- 前端 `node --test --test-name-pattern="forwarding error" tests/sessionInput.test.mjs`：3 通过，通过真实 hook 和 store 验证当前错误结束运行态、不发送排队输入、旧请求不影响新执行、非终态不结束运行态。
- 扩展运行 sessionInput.test.mjs 与 chatErrorSseLifecycle.test.mjs：118 通过、6 失败、1 跳过。将 hook 恢复到未修改基线、保留新增回归用例对照：116 通过、8 失败、1 跳过；新增用例中的当前终态收尾、旧请求隔离均在基线失败。共同 6 项失败涉及已有 SSE 测试替身缺少 isTeamOrganizationUiEnabled 等依赖及 MoA 断开清理断言，不纳入本次修复。
- 未运行格式化、静态检查、全量 UT、真实 Windows10 0.2.9 安装包 UI 回放；上述证据不能替代现场切换/重启验收。

## 遗留与重验条件

修复防止本次调度饥饿触发的回放溢出，不保证任意流量或慢 Gateway 永不溢出。真正转发失败后的历史错误留存、完整任务快照恢复不在本次最小变更内；现场需复验 #6229/#6230 的切换与重启表现。转发、IPC 或请求身份处理发生后续变化时重跑对应定向用例。
