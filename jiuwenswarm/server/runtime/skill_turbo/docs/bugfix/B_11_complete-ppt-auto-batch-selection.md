# 完整 PPT 请求自行关闭自动组批

当前完整生成策略已由用户确认改为四段一批；本文件保留此前两批修复的历史依据。
当前规约见 S_12/S_13，实施演进见 F_11「默认四段连续执行、按需审阅」。

| 项 | 值 |
|---|---|
| 日期 | 2026-11-27 |
| 类型 | bugfix |
| 分支 | fix/ppt-segment-file-handoff |
| 关联规约 | S_12；既有执行语义见 S_13 |
| 关联特性 | F_11_declaration-driven-auto-batch.md |

## 现象与根因

六页时间管理培训任务 officeclaw_dd5751dacac474ea5d9992c2 在完整生成需求下，分别调用
prep、outline、generate、export，并均显式传 auto_batch=false；对应 history.json 的工具调用位于
16、145、202、398 行。已有默认组批正确执行了参数要求，未发生引擎漏展开。
模型另建四个阶段 todo，大纲中途读写又增加模型回合；外层共 15 轮，对照单入口为 4 轮。
旧指南给出三态与“优先组批”，但没有明确禁止将模型自己的阶段计划当成提前停止的依据。

## 修复与边界

- 中英文指南给出完整请求的 prep/true、generate/true 两个最小 JSON 调用示例；同一需求复用一个交付物 todo。
- 明确大纲检查在 prep→outline 链尾完成，批成功后不补调已经完成的 outline 或 export。
- 同步真实模型工具描述、系统指南、根技能附录和 Relay PPT turbo 描述/文档。
- 用户只准备、只生成不导出、等待大纲批准或指定旧入口仍按既有范围执行；不在引擎中根据自然语言覆盖 false。
- 省略/null、显式列表、批开关、HITL、前提校验和发送防重语义保持不变。

## 验证

候选基于 Jiuwenswarm HEAD 903e1976a、Relay HEAD 8f418c87bd 及任务开始前已有的未提交自动组批改动。
新增 SDK 载体回归在修复前 4 failed，原因为完整两批示例未披露；修复后同一断言继续核对
Deep/Flash × 中英文的实际 ModelCallInputs.tools、系统 section 和加载后的 ToolMessage。

- Python 3.13 虚拟环境运行整个 `tests/unit_tests/server/runtime/skill_turbo/`：840 passed，47.32s。
- Relay `test_segment_auto_batches.py` 与 `test_turbo_registration_gate.py`：11 passed，176.75s。
  新增中英文示例分别经真实 SDK 工具 invoke 执行两批，独立读取成品确认 6 页、两批同一任务且仅一次发送；
  模型和发送边界使用替身。既有测试覆盖默认/null/true、显式 false 的范围限制，以及审阅原批恢复。
- Relay `pnpm check` exit=1，8236 个既有格式错误，首项为未改动的 biome.json；
  `pnpm lint` exit=1，未改动 green-package/xlsx-editor 的依赖解析及类型错误。
  日志：`%TEMP%/ppt-auto-batch-policy-check.log`、`%TEMP%/ppt-auto-batch-policy-lint.log`。
  未修改这些文件或自动格式化。PR gate 仅支持 commit 范围，本次未提交，未把 HEAD 检查当作工作区验收。

以上 SDK/引擎测试不证明真实模型一定遵循指引。未重启服务、未进行真实模型或前端验收，
没有据此承诺 token 节省比例。代码保留在本地，未提交或推送。
