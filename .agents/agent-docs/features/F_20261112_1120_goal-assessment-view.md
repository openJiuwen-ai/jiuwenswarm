---
id: F_20261112_1120_goal-assessment-view
title: Goal 评估轨迹独立展示与回放隔离
type: F
author: alan_cheng
author_email: chengshuo5@huawei.com
created: 2026-11-12
updated: 2026-11-12
modules:
  - jiuwenswarm/observability
  - jiuwenswarm/channels/web/frontend/src/features/trajectory
files:
  - jiuwenswarm/observability/retention.py
  - jiuwenswarm/channels/web/frontend/src/features/trajectory/GoalAssessments.tsx
  - jiuwenswarm/channels/web/frontend/src/features/trajectory/projector/otel-trajectory-projector.ts
  - jiuwenswarm/channels/web/frontend/src/features/trajectory/projector/attribute-resolver.ts
  - jiuwenswarm/channels/web/frontend/src/features/trajectory/trajectory/model.ts
  - jiuwenswarm/channels/web/frontend/src/features/trajectory/client/TrajectoryExplorer.tsx
designs: [agent-core:D-20261112-1120-goal-assessment-isolation]
---

# Goal 评估轨迹独立展示与回放隔离

## 依据与决策关系

用户确认的跨仓方案由 agent-core 的 D-20261112-1120-goal-assessment-isolation 管理；core 维护 callback-only 用途及主窗口不变量。本仓维护 [S_01_goal-assessment-trajectory](../specs/jiuwenswarm/observability/S_01_goal-assessment-trajectory.md)。
本次在 codex/team-leader-goal / 52abae5b98329f7e164055913936d8c82f662e52 的干净工作区开始，仅新增观测隔离；此前调度模式 leader goal 开关及默认最大次数、无 token 预算的决定继续有效。

## 实际实现与取舍

- 由 core 真实调用链提供 purpose=goal_assessment 与 goal id、attempt、assessment id、strategy；通过生成脚本同步 semconv，不手写生成文件。
- 纯 projector 在输入差分前分出评估 inference 及 reasoning；独立评估各自拥有输入基线，主执行请求数组与步骤不混入评估。Goal 唤起执行仍保留在 leader 轨迹，并标 Goal #N。
- leader 原泳道下增加默认折叠的评估区，复用 TrajectoryTable，完整查看 SYSTEM、输入、输出及原始 OTel。展示模型结论、usage 和耗时，未知结论或失败不冒充目标完成；GoalManager 仍是状态权威。
- retention 保留真实物理请求偏移，过滤评估 lineage seed；会话总 usage、原始记录和所有者不变。
- 未创建伪 teammate，也没有仅靠提示词文字猜测用途或回写历史；仅前端隐藏无法解决错误窗口链，故在 core 生产方同步修复。
- 中英文用户说明及 MIT 来源 PROVENANCE 同步。与已确认目标方案无偏差。

## 验证与证据

环境为 macOS arm64，Swarm Python 3.13.5，core Python 3.11；Swarm 使用 link-core editable 指向 /Users/alan/Developer/jiuwen/agent-core（4836fc2815b897bf022e36d5df22481fb446047d + 本任务差异）。所有 Python 执行前初始化 venv / PYTHONPATH。

| 范围 | 实际入口 | 结果 |
| --- | --- | --- |
| 输入差分、评估重试/失败、多轮、实时/回放 | npm run test:trajectory-projector | 98 passed |
| 保留、窗口和 subject | trajectory retention / window Node 套件 | 9 + 60 passed |
| 宿主、泳道、时间轴 | trajectory host / lanes / timeline Node 套件 | 20 + 16 + 22 passed |
| SQLite 保留及 processor | pytest observability/test_trajectory_retention.py、test_content_addressed_storage.py、test_core_processor_integration.py | 42 passed |
| 构建与启动 | make restart-debug（前端 build + 服务启动） | 成功；5173 页面可达 |
| core 关联测试 | core F_20261112_1120_goal-assessment-isolation | 最终 417 passed, 1 skipped；包括 Goal typed RunContext 回归 |

真实 UI 会话 web_1a1cdcb1b67_4652dffd21c0 明确启用 scheduled，使用 deepseek-v4-flash，两阶段任务交付 numbers.txt=1/2/3 和 result.txt=sum=6。独立读取文件核对，不以模型声称完成代替副作用验证。
首次原始 SQLite 显示一个 chat 评估请求及其 reasoning 子 span，均带 purpose=goal_assessment / attempt=1 / strategy=hybrid，保留原 leader owner；评估没有 context.window.commit 子记录。UI 显示独立评估 1 次、28786 tokens、7811 ms，输出为 complete；检查器 OTel 可打开。
负向对照 web_1a1cdc8863d_718d2818680d 是 autonomous：完成普通任务，无 goal_assessment 标记。
真实验收发现初版比较 RunKind 枚举与字符串导致执行轮无 Goal 标记；core 改为可信枚举及 normalize_run_context，增加 enum/string/normal 三类回归。旧已采集会话不回填标签。
重启后的同会话只读核验于 11:34 完成，采集到第二个新 goal 的两次执行模型请求，均带 goal id=e8a928fe6eb3 / attempt=1；第二次独立评估同样没有 context.window.commit。刷新回放后显示两次独立评估合计 55274 tokens，第二次评估 26488 tokens / 5002 ms，结论 complete；Raw 与 OTel 检查器可打开。
组件通过既有 TrajectoryTranslate props 获取文案，保持 client 无应用 i18n context 依赖；中文资源及独立英文默认字典均完整提供。

日志在 /tmp/goal-assessment-*.log 与 /tmp/goal-observation-core-broad.log，截图 /tmp/goal-assessment-live.png 为本地验收材料，不提交用户原始轨迹或日志。

## 覆盖与限制

已追通 assessor → callback → SQLite → projector → leader 泳道及 retention 消费链。旧无用途标记数据保持原样；多轮、失败与重试通过可控数据验证，真实模型本次一次评估即完成，不宣称真实模型发生了 continue 自动续跑。
未执行全仓测试、Windows/Linux、Chrome 107 真机、性能与安装包验证。未运行 formatter/lint/mypy 等静态检查。
本地 pyproject.toml / uv.lock 的 editable 切换仅用于联调，不能作为发布依赖提交；需先合入 core 再更新远程固定 pin。本次提交分属 core 独立修复分支与 Swarm 现有 PR；core 合入前，本 PR 的新观测生产方依赖仍待更新，不能把 editable 联调结果视作固定依赖验收。后续修改生产方契约、retention 或装配时应重验相关链路。
