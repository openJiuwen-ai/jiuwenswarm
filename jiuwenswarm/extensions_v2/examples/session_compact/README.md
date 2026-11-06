# Project Compaction 示例（接口阶段，真实压缩未接入）

依赖本次 Core 压缩 Hook 与 Server session.compact 实现。安装 ZIP 时 manifest.json 与 extension.py 位于包根目录。包由 Worker 加载，不需要改前端或调用 ui.render。

默认只注册 `session.compact` 策略。当前生产主流程的输入前引擎绑定、手动/自动压缩桥接与任务结束预计算调度均已注释，因此原 command.compact、正常 query、自动阈值和溢出恢复不会调用这个策略。

若将 create_extension 改为 `return ProjectCompaction(compact_on_attach=True)`，on_attach 仍可使用已经登记的 Ops 代理；在当前生产环境会明确得到 `COMPACT_NOT_INTEGRATED`，示例记录原因后继续原 attach 链，不会修改会话消息、调用摘要模型或提交存储。仅安装示例或修改预计算配置都不会启用真实压缩。

完整 Ops→Event 的验证独立于生产流程：`tests/unit_tests/extensions_v2/test_session_compact.py` 手工装配 Bus / Runtime / Builder，显式绑定受控引擎，再从 USER 调用 Ops，观察 session.compact 的 USER→可选 BUILTIN→CORE→continuation 往返。`test_session_start.py` 的真实安装包用例验证默认生产装配仍可调用 Ops 并得到 NOT_INTEGRATED。测试绑定不属于用户层绕过平台收集上下文的接口。

方法只填写 instructions 字符串或省略；不填写 session_id、trigger、agent_id、messages、bus 或回调。trigger=plugin 和目标主会话由平台绑定。结果分为 SessionCompacted(messages, tokens_before?, tokens_after?) 与 SessionCompactSkipped(skip)。先判断分支，再读字段；缺失 Token 数不等于 0。

不要在 on_compact 内再调用 compact Ops。压缩 Hook 内要继续默认压缩，调用 next_handler；要跳过本次，返回 SessionCompactSkipped；要保留原生消息，保留该次 handle。方法调用结束后得到的消息不携带可供后续调用复用的句柄。

详细输入输出、字段示例、修改权限和错误见 [F_28_session-compact.md](../../docs/features/F_28_session-compact.md)。

平台实现源码：[service.py](../../session_compaction/service.py) 承接 Ops，[dispatch.py](../../session_compaction/dispatch.py) 进入事件与可信 continuation；用户包继续使用现有 Ops 和公开事件类型。当前真实主流程仍未接入。
