> 历史评审记录：以下额外机制及其专用测试已在最简版移除。当前实现见 [最简方案](duplex-simplification.md)。

# 第二轮评审修复

后续已按评审删除额外收件机制和硬编码限制，当前行为见 [简化说明](duplex-simplification.md)。以下验证记录对应删减前提交。

针对 `duplex-review-ab595cc9.md` 中 F1–F4。先看[五步总览及分层评审入口](diagrams/README.md)。

| 问题 | 改动 | 验证场景 |
| --- | --- | --- |
| F1 旧摘要覆盖新计划 | 摘要绑定发布时的目标、计划和当前任务；来源改变后失效，快照读取当前已提交计划 | 原生 Native 更新计划后，经 DB 触发快模型；旧目标、旧假设和旧动作不再进入 HTTP 请求 |
| F2 已读消息只在内存中 | active 路由先同步持久化完整输入；上下文与采纳 ID 一起保存；恢复身份含会话及 Agent，或由评测显式绑定 | 原 DAO ACK 后强杀；新进程首个模型请求保留原任务、新消息和已完成工具结果 |
| F2 工具成功与上下文提交之间崩溃 | 工具收据同时保存执行后的计划与摘要状态，恢复按收据提交版本补回结果和状态 | 收据提交后、工具消息写入上下文前强杀；恢复不重复执行已完成副作用 |
| F3 网页动作游离于恢复链 | 官方更新走原用户输入处理器；网页动作先写同一工具账本，原 `env.step` 后追加原轨迹，再提交动作证据并关联 Native 检查点 | 真实 Chromium、原 `ScriptBrowserEnv.env.step` 点击后重启 Native worker，页面计数仍为 1 |
| F4 U2A 缺安全全量打断 | `always_interrupt` 贯通 runner/worker CLI、默认实验策略及原用户入口，复用安全恢复实现 | 真实入口接受该策略；慢模型被取消，快模型调用数为 0；与 `abort_restart` 分开 |

额外修复：显式 stop / pause / abort 保存停止标记，晚到的检查点不能覆盖它；重启不会自动复活已停止任务。输入覆盖依据来自运行时采纳元数据，不能通过消息正文伪造。无来源信息的旧摘要不作为新状态使用。

## 恢复边界

- 恢复需要相同稳定身份和原持久文件。受保护的是 active `RoutedInput` 路径；没有消息 ID、没有外部 ACK 契约的直接 `Native.send(str)` 不承诺入口确认持久化。
- 网页动作证据不等于浏览器镜像。同一存活浏览器可继续；换了环境或动作结果不明时停止恢复，必须先恢复或核对外部状态，不自动重演点击。
- 无法重建的工具收据、交互式冷恢复会明确失败，不能宣称任意工具恰好一次。持久收件记录尚无清理策略。
- 原题、原提示构造、原动作解析、原评分器保留。真实本地网页故障测试不是公开 Benchmark 成绩；正式环境仍缺容器和 WebArena 站点。

## 回归文件

- [旧摘要单元测试](../tests/unit_tests/agentserver/test_duplex_intent_freshness.py)与[真实入口测试](../tests/integration_tests/test_duplex_intent_freshness_e2e.py)
- [真实强杀恢复](../tests/integration_tests/test_duplex_durable_recovery.py)、[停止和覆盖保护](../tests/unit_tests/agentserver/test_duplex_inbox.py)、[停止不复活](../tests/integration_tests/test_duplex_recovery_review.py)
- [U2A 入口、网页提交和真实 Chromium 测试](../tests/integration_tests/test_duplex_browser_recovery.py)

这些测试检查机制与故障契约，不以测试数量替代公开任务效果结论。

2026-09-08 本机合并回归：**160 passed**，218.46 秒，无跳过。使用锁定 SDK
`94e10cb`，包含全部 duplex 单元/集成测试、TeamManager registry 与命名回归，
以及原 Python 3.11 WebArena 环境的真实 Chromium 测试。一个上游 Authlib 废弃提示，
不影响退出码 0。Ruff、diff 空白检查、6 份 PlantUML 语法与 SVG 生成均通过。
日志：`D:/jiusi_agent/duplex-review-fixes-full-2.txt`。

后续测试修正（`b3f6642a`）：服务器打断可能发生在旧 HTTP 请求发出之前，原测试按请求到达顺序阻塞，误把恢复后的新请求卡住。改为按旧提示内容阻塞，并增加发送 HTTP 前取消的确定性用例；未修改生产逻辑。三组 U2A 专项全部通过；相关本机回归 **33 passed、2 skipped、1 deselected**，其中未配置的官方环境用例跳过，耗时 Chromium 用例本轮未重复执行（已包含在上述 160 项回归）。

服务器最终回归（`b3f6642a`）：**88 passed、3 skipped**，94.32 秒，无失败。原超时项和新增 HTTP 前取消用例均通过；跳过项为缺 Harbor 的 1 项、缺独立官方 WebArena 环境的 2 项。日志：`D:/jiusi_agent/duplex-remote-results/review-fixes-tests-final.log`。
