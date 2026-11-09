---
id: S_01_bounded-history-replay
title: 有界会话恢复与 Worker 崩溃保护
type: S
author: 刘瑜
author_email: liuyu524@huawei.com
updated: "2026-11-09"
modules:
  - jiuwenswarm/server/runtime/session
  - jiuwenswarm/agents/harness/common
  - jiuwenswarm/extensions/video_duplex/backend
  - jiuwenswarm/server/supervisor
  - jiuwenswarm/runtime
files:
  - jiuwenswarm/server/runtime/session/history_replay.py
  - jiuwenswarm/agents/harness/common/session_ops_service.py
  - jiuwenswarm/extensions/video_duplex/backend/conversation_context.py
  - jiuwenswarm/server/supervisor/runtime_supervisor.py
  - jiuwenswarm/runtime/events.py
  - jiuwenswarm/server/agent_ws_server.py
---

# 有界会话恢复与 Worker 崩溃保护

## 范围

约束模型上下文冷恢复、热上下文的视频输入同步、旧格式写入转换，以及 Worker 就绪后的重复异常退出。历史展示、审计导出与模型上下文恢复是不同消费者；现有展示/导出的完整读取接口不作为模型恢复接口。

## 历史读取预算

`read_history_replay()` 在会话 lifecycle 锁内读取固定文件边界，返回语义记录、截止游标、当前请求之前的游标及实际读取字节数。

| 预算 | 默认值 | 超限行为 |
| --- | --- | --- |
| 单条 JSONL 记录 | 4 MiB | 解码前拒绝 |
| 保留的语义记录原始字节总和 | 16 MiB | 拒绝本次恢复 |
| 保留的语义记录数 | 8192 | 拒绝本次恢复 |
| 单次扫描范围 | 256 MiB | 打开正文前拒绝 |
| 同时执行的恢复读取 | 2 | 等待最多 5 秒，随后报忙 |

MiB 为 1048576 字节。预算限制原始输入和保留窗口，并不代表整个 Worker 的 RSS 上限。JSONL 逐条解码，诊断 prompt、delta 等非上下文事件不累计进入恢复列表。用户消息、工具调用及结果、最终回复、推理与既有摘要/来源语义保留；不能任意截取最后 N 行。

`read_history_replay()` 的旧 JSON 数组快速路径只在整个文件不超过单条预算时读取。第二批增加的迁移与默认 JSONL 写入转换使用流式解析器，受 8 GiB / 120 秒扫描预算约束；转换保留全部记录与原数组文件，不整体解码。具体契约见同目录 `S_02_durable-context-recovery.md`。

## 失败与安装

- `HISTORY_RESTORE_LIMIT`：单条、扫描或保留窗口超限。
- `HISTORY_RESTORE_RESOURCE`：读取或消息构建发生 MemoryError。
- `HISTORY_RESTORE_BUSY`：恢复读取名额等待超时。
- `HISTORY_RESTORE_CHANGED`：游标所属文件、生命周期代次或快照已失效。
- `HISTORY_RESTORE_FAILED`：损坏记录、I/O 或上下文构建/安装失败。

资源不足不能伪装成坏行跳过，失败不能返回部分历史或空列表后继续调用模型。读取、转换与安装失败必须传到 Runtime；失败的冷适配器移出可用缓存并清理。取消也不能留下可被下一请求复用的未恢复适配器。刷新已有上下文时，安装失败恢复原上下文和 session 中的 context 状态。

超过快速路径预算的旧会话交给 `S_02_durable-context-recovery.md` 定义的受控迁移；提交的有效快照优先于历史回放。迁移也超限时明确失败。失败不会删除或裁剪原始历史。

## 视频增量同步

游标绑定文件路径、设备/inode、生命周期 generation、文件快照身份及字节位置。首次恢复给上下文记录当前请求之前的消费位置，后续只处理未消费的语音记录。

写入侧维护最多 1024 项的进程内历史头缓存。只有完整已知的文件前缀才能更新缓存；重写使缓存失效，外部变更不能被乐观判为无新增内容。缓存确认没有新增语音时，不打开历史正文。缓存缺失则走有界读取。

语音加入上下文并保存 ReAct 重载状态成功后才推进游标；失败回滚，不提交消费进度。InteractiveInput 恢复维持原有跳过同步的顺序约束。

## 其他在线消费者

错误记录去重采用流式检查，不能因错误写入再全量装载失败会话。回复建议只查看最多 1 MiB 的尾部；无法确认最新完成请求时抑制建议。可选建议不能通过全量审计历史重建新一轮内存峰值。

## 崩溃窗口与错误回包

保留启动阶段的既有重试计数和退避。另记录最近 600 秒内就绪后确认退出/被判定失效的 Worker；达到 3 次后进入 FAILED 并等待窗口冷却。AGENT_READY 不清空此窗口；正常退出不计数，活的 resident Worker 的单纯 IPC 断连不直接作为进程崩溃。Windows EOF 与 OS 退出状态的竞态最多等待 250 ms 确认。

自动恢复不会在冷却期限之前重启；既有显式配置重载恢复入口保留。Worker 关闭后不自动重放可能已有工具副作用的用户输入。

Supervisor 合成错误携带原请求和会话身份；熔断中的直接请求返回 `WORKER_UNSTABLE`。经过 Front admission 的请求仍遵循既有 `RUNTIME_FAILED` 契约。Runtime 保留异常的字符串 code，AgentServer 编码时保留 code 和会话身份。流式执行异常仍使用既有 unary E2A 错误终态，保持 `is_stream=false`、`is_final=true`、`status=failed` 的兼容约定。

## 验证依据

回归用例位于 `test_history_replay.py`、`test_worker_crash_window.py` 及已有 warmup、prefetch、permission、prompt suggestion、history cursor/write 和自动恢复测试。故障与真实进程证据见 `B_20261106_1618_issue6134-bounded-history-recovery`。
