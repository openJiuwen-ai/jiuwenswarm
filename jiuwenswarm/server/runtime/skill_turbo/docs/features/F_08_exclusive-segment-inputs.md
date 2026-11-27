# 互斥页面来源的输入装配

| 项 | 值 |
| --- | --- |
| 类型 | feature |
| 日期 | 2026-11-15 |
| 范围 | 第五步必要引擎契约修正，仅 segment_inputs.py |
| 状态 | 本地实现及回归通过，未提交或发布 |
| 关联规约 | [[S_10]] |

## 依据与范围

按用户确认的《Skill多入口-第五步-PPT四段文件交接与验收执行方案》实施；主要业务修改在 relay 的 `office-claw-skills/pptx-craft/turbo/`。在两仓当前分支上建立 `fix/ppt-segment-file-handoff`，保留前四步及用户既有工作；修改前文件哈希、Git 状态与副本保存在 `%TEMP%/ppt-segment-step5-baseline-ejd2ew2e`。

原五层逐键装配会让显式页面列表与台账页面清单同时出现，破坏二选一；None 被普通显式参数规则忽略后还可能偷偷退回历史来源。修正仅针对由单字段 required 分支组成的 oneOf：从原 params 键集合选源，在路径、前提和 schema 校验前投影本次输入。显式两个键拒绝；显式单键保留无效原值，让校验拒绝；无显式键仅继承第一个分支的有效、已验证上游事实。禁止继承历史第二分支作默认，不改写历史事实或文件。引擎没有硬编码 PPT 字段名。

恢复继续验证原 inputs，不重新选源。未增加 force、批次、发送 API、交付 UI 或新的状态存储机制；validator 和 runtime 门面不变。

## 验证

新增 `test_segment_exclusive_inputs.py` 的 12 项行为测试；连同原 `test_segment_inputs.py` 为 36 passed。后端 SkillTurbo 及实际工具/HITL 相关回归为 754 passed，日志 `%TEMP%/ppt-step5-swarm-regression.log`。测试检查显式双键、空值、空列表、互斥历史来源、默认来源、越界的未选中历史路径和原 facts 保留。

遵循 jw-dev 不运行 jiuwenswarm 格式化或静态检查。PPT 真实文件验证、relay 检查结果与后续步骤边界见技能侧 `turbo/STEP5_VERIFICATION.md`。本次引擎新增内容仅本文件、S_10 修订、segment_inputs.py 的选源逻辑与对应测试；不把前四步未提交改动计为第五步新增。

## 提交边界

尚未提交。需要提交时依模块约定，将引擎特性代码、测试、文档拆为相邻三个提交，并把前四步既有内容单独核对后交由用户决定，不自动混入提交。
