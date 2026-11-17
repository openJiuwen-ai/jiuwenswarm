---
id: B_20261117_1129_history-migration-cancel-pagination-029
title: 0.2.9 历史迁移的取消工具分组与记录数分页
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/server/runtime/session
files:
  - jiuwenswarm/server/runtime/session/context_migration.py
  - jiuwenswarm/server/runtime/session/history_stream.py
  - tests/unit_tests/test_context_migration.py
designs: []
design_note: 回补已确认的 develop 修复，属于既有历史恢复契约的局部修正，无独立设计。
---

# 0.2.9 历史迁移的取消工具分组与记录数分页

## 现象与影响

取消请求的工具调用没有对应结果时，后续本来闭合的小工具组会被合并进同一迁移页，最终触发 `HISTORY_RESTORE_LIMIT`。另一个独立场景是 2050 条小记录总量不足 512 KiB，却先超过原先 2048 条硬上限，导致整次恢复被拒绝。

本次将 [develop PR !10360](https://gitcode.com/openJiuwen/jiuwenswarm/merge_requests/10360) 的三文件修复回补到 `releases/0.2.9`；生产代码和回归用例沿用同一实现。0.2.9 基线为 `745377ed18490cabfd7008313d598f6e0331ebc5`。

## 关联设计与当前契约

未找到该历史迁移模块的专门 spec；以 `session_history.enqueue_history_request_completion` 的完成事件、`HistoryReplayError` 的失败语义和现有恢复测试为依据。

完成标记由宿主在请求输出入队后持久化；历史是审计事实，迁移页是重建有效上下文的临时分组。冷恢复由 `warmup_session_context` 在有界读取超限或存在旧自动摘要时进入迁移，增量检查点恢复也会在尾部超限时进入同一迁移函数。迁移成功后才保存检查点，失败回滚并保留原历史。

## 根因与修复决策

- 流式读取原先没有保留非语义行的 `status` 元数据，分页器又在检查取消终态前调用 `is_context_record`，所以取消未释放 `open_calls`。
- 按 `request_id + tool_call_id` 跟踪未闭合调用；只在显式 `chat.request_completed(status=cancelled)` 时结束该请求的分组。其他请求、等待用户或未知状态不能被当作取消；旧配对缺少请求字段时仅接受唯一匹配。
- 取消边界不进入模型消息。既有消息重建器过滤无结果调用，保留已完成结果与 reasoning，不伪造结果，不改写审计文件。
- 工具组闭合后，512 KiB 或 1024 条任一软阈值触发分页，为并行工具结果预留空间；4 MiB / 2048 条硬上限保持有效。

实际排除的方案：不清空所有请求的未完成调用，避免破坏并行组；不凭新用户消息推断旧调用取消，因为补充输入可能插入工具执行期间；不删除原记录或提高硬上限来绕过恢复失败。

## 回归证据与适用范围

环境为 Windows / Python 3.12.10。使用独立测试环境，Core 为 0.2.9 固定的 `45d53dd1919a09e3801ce7bca3798f776ac72ca1`，Gateway 为 `1119ea00f7d5829370f6c0d38f6f24a012f46be7`，协议包为 `267c077fa832b25f7f64a913461c2124353bc911`；未修改依赖声明、锁文件或 IPC 版本。

定向命令：

```text
python -m pytest tests/unit_tests/test_context_migration.py tests/unit_tests/test_context_snapshots.py tests/unit_tests/test_history_review_regressions.py --no-cov -q -o log_cli=false
```

新增 20 项用例在候选上全部通过：覆盖 JSON/JSONL、取消孤立调用后的 60 个正常工具组、2050 条小记录、并行调用、插入输入、请求隔离、旧配对兼容、硬上限和当前请求排除。实际 Core ContextEngine 完成迁移及检查点重新加载，独立比较原历史文件逐字节不变。

同条件基线加入相同回归用例后为 **95 passed / 9 failed**，9 项失败均位于新增迁移回归；应用候选后为 **104 passed**，无跳过或失败，原有 84 项也全部通过。本地证据保存在 `history-migration-cancel-pagination-029-20261117` 验证目录的 `baseline.xml` 与 `candidate.xml`，不随代码提交。

初次测试环境未展开基础环境的 PyWin32 路径，导致扩展适配器测试缺少 `pywintypes`；已修正独立环境并重新执行，初次结果单独保留，不作为产品回归结论。

压缩调用使用确定性替身；以上不证明真实模型质量、原现场长期稳定性或 OOM 根因。记录格式、依赖 pin、分组状态机或消息重建器变化后需重验。

## 防错模式与资料同步

取消是请求范围的终态，不能由内容过滤器丢失；分页必须同时考虑字节与条数，并在工具组安全边界切页。这两个局部约束由生产注释和上述回归维护，不扩展成无关模块的全局规则。按仓库流程维护本记录与生成索引；检视范围为该回补差异和直接恢复调用方。

## 遗留与后续

发布分支回补、远端 PR、CI 与实际发布是不同状态；本地通过不能替代远端 CI 或发布验收。Core 已结束 trace 的大字段释放不属于本次 Swarm 回补。
