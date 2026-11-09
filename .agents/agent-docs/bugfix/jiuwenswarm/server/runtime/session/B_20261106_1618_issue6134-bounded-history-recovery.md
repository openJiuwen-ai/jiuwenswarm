---
id: B_20261106_1618_issue6134-bounded-history-recovery
title: Issue 6134 历史恢复内存放大与重复崩溃止血
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
date: "2026-11-06"
modules:
  - jiuwenswarm/server/runtime/session
  - jiuwenswarm/agents/harness/common
  - jiuwenswarm/extensions/video_duplex/backend
  - jiuwenswarm/server/supervisor
  - jiuwenswarm/runtime
files:
  - jiuwenswarm/server/runtime/session/history_replay.py
  - jiuwenswarm/server/runtime/session/session_history.py
  - jiuwenswarm/agents/harness/common/session_ops_service.py
  - jiuwenswarm/agents/harness/common/recommendation/prompt_suggestion.py
  - jiuwenswarm/extensions/video_duplex/backend/conversation_context.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/server/supervisor/runtime_supervisor.py
  - jiuwenswarm/runtime/events.py
  - jiuwenswarm/server/agent_ws_server.py
designs: []
design_note: 用户在本次会话明确确认先实现并验证第一批止血修复，无独立 D 编号；持久快照与迁移仍属于第二批。
---

# Issue 6134 历史恢复内存放大与重复崩溃止血

## 现象与证据边界

Issue 6134 报告 1.57 GB 历史和约 23.8 GB Worker RSS。用户另提供长稳包，构建对应 Swarm a2abcecc/Core 5a35c39；该包不含原始 history.jsonl、Worker stderr、内核日志或部署 wheel。早于首次报告 OOM 的集群 STALLED 不能全部解释为内存故障。

首次第一批基于当时的 upstream/develop `acf2b35b263d5813517d8b9fac187d1df0a9c017`，使用其声明的 Core `f3f8f576164cc405c046e43079c8ad00ee17f0c4`、Protocol `cbbadde9101db6e3d75e4ef2b9bdb6029fd92088`、Gateway `cff3b1b9c2dd02bf29e0cb331dec12be989bc656`。未修改依赖版本。

## 根因与修复

1. 冷恢复 read_text/split/全量解码产生多份大字符串；MemoryError 被宽泛 except 当成坏行跳过。新增严格有界读取，投影有效上下文事件，并把失败传到实际请求。
2. Web 热输入也经视频同步全读历史。采用文件/generation 游标和可重建的写入头缓存，无新增语音时读取正文为 0 字节；游标在保存上下文成功后推进。
3. 预读与 warmup 的外层捕获会吞错，部分恢复适配器可能进入缓存。资源/恢复错误与取消移除失败子实例；刷新失败回滚上下文状态。
4. 最新 develop 的回复建议也全读历史查最后一个完成请求。真实进程调用栈确认该路径后，改为有界尾部读取；错误写入去重和旧格式转换一并消除相同回退。
5. ready 后退出反复获得启动重试次数。另设 600 秒/3 次的异常退出窗口；Windows EOF 先于进程退出可见时，最多等待 250 ms 确认。第三次进入冷却，后续请求及时失败。
6. Runtime 包装异常会丢失 code，unary E2A 错误会丢失 session_id。补齐字符串 code 和会话身份，保留既有 unary 错误终态兼容性。

当前有效预算、错误与恢复约束见 `S_01_bounded-history-replay`。

## 取舍

- 未选择仅把 read_text 改成逐行后仍累计全量列表：保留窗口仍然无界。
- 未选择只看当前请求渠道决定是否同步语音：会破坏视频与文字共享上下文。
- 未选择继续跳过 MemoryError 或简单取最后 N 行：会隐性丢失消息或拆散工具调用。
- 未选择在 AGENT_READY 清空全部崩溃记录，也未把活 Worker 的 IPC 断连直接记作崩溃。
- 未选择自动重放失败用户输入：工具可能已经产生副作用。
- 超预算旧会话明确失败，保留原始历史；透明继续对话依赖第二批快照和迁移。

## 真实进程验证

证据根：`D:/work_space/.codex-temp/issue6134-phase1-20261106/`。使用 Windows、Python 3.11.15、真实 RuntimeSupervisor/Worker/适配器与本地确定性模型，原生 Windows Job Object 限制 Worker 私有内存为 1536 MiB。没有使用真实模型服务，没有自然触发 Linux 内核 OOM。

同一 develop 基线、相同 64.21 MiB 合成 JSONL、相同依赖和内存上限：

| 场景 | 基线 RSS 峰值 MiB | 候选 RSS 峰值 MiB | 候选私有内存峰值 MiB |
| --- | --- | --- | --- |
| 冷恢复 | 1051.51 | 415.08 | 897.57 |
| 热输入 | 1051.55 | 415.62 | 897.92 |

基线触顶并产生逐行 MemoryError；候选保留完整有效消息，模型收到旧问题、旧回复和已有摘要，热语音同步的两次检查均为 0 字节。保留原文与摘要同时恢复的行为属于第二批问题，本次没有宣称已修复。

18 MiB 有效消息超出保留预算，以及单条 5 MiB 消息超限时，失败请求均无模型调用；同一个 Worker 随后的正常会话回复成功。三次在真实历史读取期间主动终止 Worker 后，状态进入 FAILED；第四个直接请求返回 WORKER_UNSTABLE，未启动第四个 Worker。

首次虚拟环境启动器采样落在外层 launcher PID，该轮内存结果作废。后续使用直接 Python 启动，断言采样 PID 与 Supervisor 的实际 Worker PID 一致，并在终止前复核 PID、创建时间和父进程。首次退出确认实现漏掉 Windows 的 EOF/进程退出竞态，失败记录保留，修正后重跑通过。

## 单测、检视与交付

定向回归覆盖有界读取、坏行/MemoryError、增量语音和回滚、prefetch/warmup、工具消息配对、fork/rewind、权限冷恢复、回复建议、历史读写游标与两种 IPC 模式的自动恢复/崩溃窗口。最终执行结果写入证据根的 `regression-final.xml` 和交付报告。

Windows 单测使用外部退出插件，在上游 tests/conftest 删除临时目录前关闭测试进程日志句柄；该插件不属于产品补丁。未运行格式化或静态检查。代码与文档仅为本地工作区差异，未提交、推送或创建 PR。

## 遗留

本节保留第一批完成时的验证边界。2026-11-06 第二批已实施持久快照、压缩边界回放、旧大历史迁移和原始负载外置；当前契约见 `S_02_durable-context-recovery`，第二批证据见同目录 `B_20261106_1803_issue6134-durable-context-recovery.md`。以下未实施说明不再代表当前候选。

第二批的持久化有效上下文快照、压缩边界回放、旧大历史迁移、完整 prompt/大结果外置尚未实施。前端等待器与 OOM 前集群 STALLED 属于后续独立工作。未进行 8 小时真模型长稳、原 1.57 GB 历史回放、Linux、安装包或远端 CI 验收。
