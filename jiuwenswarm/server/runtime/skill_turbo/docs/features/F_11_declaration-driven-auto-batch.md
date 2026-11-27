# 声明驱动自动组批

2026-11-27 迁移到新 dev-stable 的版本、SDK 前提及联合回归证据见 [[F_12]]「dev-stable 迁移验证」；下文保留各轮原始验证记录。

| 项 | 值 |
|---|---|
| 日期 | 2026-11-24 |
| 状态 | 已实现；真实服务模型验收待运行 |
| 关联规约 | [[S_02]]、[[S_11]]、[[S_12]]、[[S_13]] |

用户确认按自动组批方案实施；Relay 修改限定在 PPT turbo 目录，目录外修改须另行审核。
已有显式 accelerators 可串行执行，但真实任务中模型仍逐段调用，提示中的批量优先没有稳定落实。

新增可选 batch.auto_next 和工具 auto_batch。宿主从明确起始段构建连续列表，起始 params 不共享给后段，
继续复用已有批前校验、台账、执行租约和断点。保留显式单段语义，不隐式扩大调用范围。
PPT 仅声明 prep→outline、generate→export，保留大纲检查及改稿边界。

2026-11-24：现有 Python 3.13.14 虚拟环境运行整个 tests/unit_tests/server/runtime/skill_turbo，
803 passed（36.89s）。覆盖真实工具 schema/invoke、Deep/Flash 披露与开关、起始参数隔离、
非法链接、批上限、审阅/澄清停点、失败无后继建议、原批恢复及调用/定义漂移拒绝。

技能侧通过真实引擎、PowerShell、SVG 转换及 PPTX 合并验证两批执行、大纲改稿续接和发送不重复；
独立复测 auto_batch 与注册安全门共 6 passed（29.62s）。最终联合运行自动批次、原有显式批次、
四单段对照和注册安全门，14 passed（242.15s）；共使用真实 PPTX 文件核验改稿、产物、审阅恢复和不重复发送。
模型与发送接口使用替身；未重启服务或发真实任务，不等于真实模型/前端验收。

Relay 仓库级 pnpm check 因未改动文件的格式问题失败（8236 errors）；pnpm lint 因
green-package/xlsx-editor 中未改动的依赖解析和类型错误失败。未扩展本次范围修复这些问题。
PR gate 只接受 commit 差异，本次尚未提交，未将 HEAD 基线检查冒充工作区差异验收。

实现保留在本地工作区，未提交或推送；Relay 修改只在 PPT turbo 目录。

## 2026-11-25 默认模式三态修订

用户确认有意改变公开工具指定普通起始段但省略执行模式的语义：auto_batch 为 bool | None = None；
省略/null 默认沿声明组批，true 明确组批，false 精确单段。旧单段调用者须显式补 false。
本节局部替代上文「不隐式扩大调用范围」及原省略模式；其余声明、参数隔离、边界与发送保护继续有效。
显式列表、__full__、仅 skill 和旧 query 在省略/null 时保留原分流，true 的冲突组合不猜测用户选择。
批量开关关闭时默认调用保留单段能力，显式 true 仍被阻断；底层单段和批次 API 不改默认语义。

模型负责判断用户限定范围（包括「先做某一步」）、中间检查与后续决策需求；公开工具按现有
batch.auto_next 确定连续范围，不按 order 推断。PPT 两条链不变，目标为两次调用并保留大纲改稿边界。
中英文工具描述、请求级指南、技能加载附录与 Relay PPT turbo 说明同步三态规则。

验证从真实 SDK 工具 invoke 进入，覆盖默认/null/true/false、无后继、显式列表不扩展、__full__、
仅 skill、参数冲突、批量开关、原批暂停恢复与无新增发送不确认交付。技能侧将两批改稿/发送用例
升级到公开工具入口，保留真实 PowerShell、SVG 转换和 PPTX 合并；模型与发送仍使用替身。
本次全模块回归发现新默认路径在缺失工作区时落成普通 failed，已补入口校验为 blocked/workspace_missing，
保留原请求工作区边界断言，未从内部目录补值。

当前工作区验证（基于 fix/ppt-segment-file-handoff 的既有未提交自动组批改动，Python 3.13.14）：

- 先红：新增公开工具默认/null/链式用例在旧二态实现上 5 failed、3 passed，失败为旧单段分流、null 拒绝及缺少 nullable。
- 引擎：`.venv/Scripts/python.exe -m pytest tests/unit_tests/server/runtime/skill_turbo/ -q -o addopts='--asyncio-mode=auto' -o log_cli=false --tb=short --show-capture=no`，826 passed，33.23s。
- Relay 技能侧：使用同一 venv 运行 `test_segment_auto_batches.py` 与 `test_turbo_registration_gate.py`，9 passed，92.59s；省略/null/true 分别验证两次公开调用、改稿生效及仅一次发送，false 验证准备和页面范围限制。
- Relay `pnpm check` exit=1，8236 个既有格式错误（首项为未改动 biome.json）；`pnpm lint` exit=1，未改动 green-package/xlsx-editor 的依赖解析及类型错误。未运行自动格式化或扩大修复范围。
- 检查日志：`%TEMP%/relay-auto-batch-tristate-check.log`、`%TEMP%/relay-auto-batch-tristate-lint.log`。PR gate 仅接收 commit 范围，本次未提交，不能作为工作区验收。

未重启现有服务、未进行真实模型/前端验收、未提交或推送。以上通过结论限于本次工作区与当前 SDK、
模拟模型和发送边界；部署后仍需用真实请求核对两批工具调用与前端交付。
