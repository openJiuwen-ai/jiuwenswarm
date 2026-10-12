# 科研工作流运行记录

阶段顺序和每阶段必须留下的产物，取自本项目科研 harness 的正式六阶段登记。
本目录的代码是这套登记在 JiuwenSwarm 上的执行记录。评审对代码即可，不必另找说明。

## 阶段

| 阶段 | 门 | 不齐就不许离开 | 拒绝后回到 |
|---|---|---|---|
| init | approval_gate | topic_brief | — |
| build | coverage_gate | literature_map, paper_pool_snapshot, citation_expansion_report, acquisition_report | init |
| analyze | approval_gate | evidence_pack, claim_candidate_set, direction_proposal | build |
| propose | adversarial_gate | adversarial_resolution, study_spec | analyze |
| experiment | experiment_gate | experiment_code, experiment_result, verified_registry | propose |
| write | review_gate | draft_pack, final_bundle, process_summary | experiment |

`WorkflowRunLog.advance()` 在产物不齐时写入 `decision=refused` 和缺的产物名，阶段不前进。产物没有可打开的 ref 时，`record_artifact()` 直接拒绝，不入库。

运行中断后用 `WorkflowRunLog.resume()` 从日志本身续跑：产物、阶段与已用 token 全部由日志重建，崩溃截断的最后一行会被切掉，中间行损坏则报错。

构造时传入 `token_budget`，每次模型调用经 `record_usage()` 记账；累计超出预算后，`advance()` 以 `reason=token_budget_exceeded` 拒绝推进。同一阶段反复被拒时，`fall_back()` 把运行退回该阶段登记的回退阶段，已记录的产物保留。

## 同一次运行里的另外两道门

`delivery_gate.delivery_ready()` 在交付前检查三件事，结果经 `record_delivery()` 写入同一份 jsonl：

- 每个 `\cite{key}` 都有参考文献条目，条目带 DOI 或 arXiv id。
- 正文里印出的每个数字都在 verified_registry 里，并指向测出它的产物。
- 正文里没有 TODO、占位符、未填作者槽。

传入 `certificate` 时再加一项：审稿用的审计器必须持有认证（下一节）。

## 审计器认证：审稿说"没问题"之前，先证明它找得到问题

`auditor_certificate.py` 是 SciRigorBench 论文方法在本框架里的实现，不调模型：

1. **声明宇宙**：experiment 阶段登记的每个印出数字是一个 slot。数值彼此相撞的 slot 剔除，剩下无碰撞子宇宙 U'。
2. **随机植入**：带种子的抽签每轮选 K 个 slot，把值按 δ 改动、保留印出精度；植入清单先算 SHA-256 承诺，再开始审计。
3. **盲审**：审计器只读植入后的稿件，报出它指控的数字。
4. **精确检验**：在"审计器分不清植入与真值"的零假设下，命中数精确服从超几何分布，得到本轮 p 值；什么都指控的审计器 p=1。
5. **跨轮累积**：p 值换成 e 值连乘，任意时刻停下都有效；达到 1/α 即认证。
6. **误指控上界**：保留 slot 上的指控给出审计器冤枉真值概率的任意时刻有效上界 u0。

`scripts/certify_auditor.py` 在真实稿件的登记表上认证框架自带的两个审计器。对本论文早期版本（151 个可定位数字，U' 39 个；20 个植入轮、20 个无植入轮）：

| 审计器 | 召回 | 拒绝 | 认证 | 40 轮误指控 | u0 |
|---|---|---|---|---|---|
| 交付门数字核对（修复前） | 1.00 | 9/20 | 第 4 轮 | 310 | 0.438 |
| 交付门数字核对（修复后） | 1.00 | 20/20 | 第 2 轮 | 0 | 0.081 |
| `RigorAuditRail` 算术地板 | 0.00 | 0/20 | 否 | 0 | 0.081 |

修复前的缺陷是认证找出来的：登记表按 LaTeX 写成 `0.28\%`，正文 token 是 `0.28`，每个转义百分号都被判为未登记。这个缺陷不抛异常；单看一份报告，分不清被指控的数字是稿件的问题还是核对本身的问题，认证的无植入轮把它量了出来。

`ExperienceBaseBuilder` 只接收 `verdict_gate.action == "accept"` 的轨迹。同一模型给自己打的通过，没有这条记录就不能进经验库。

## 日志里一条记录长什么样

```json
{"event": "stage_advance", "stage": "init", "decision": "refused",
 "gate_type": "approval_gate", "missing_artifacts": ["topic_brief"], "next_stage": null}
```

```json
{"event": "delivery_gate", "decision": "refused",
 "findings": ["dangling_citation: missing is cited and has no bibliography entry"]}
```

挂载点是 `build_member_rails()` 和单 agent 的 `_build_agent_rails()`。两条链都装 `GovernanceReviewRail` 和 `RigorAuditRail`。只在 `rails/__init__.py` 导出不算挂上。

## 团队如何使用这套登记

Team Skill `research-paper-team`（位于 workspace skills 目录）把六个阶段分给六个角色：coordinator、librarian、analyst、experimenter、writer、reviewer。coordinator 用 `WorkflowRunLog` 登记产物并推进阶段，reviewer 用 `delivery_ready()` 把守交付。这个 Team Skill 通过框架自带的 `handle_skills_team_skills_hub_validate` 校验。

## 已有运行记录的导入

`research_run_import.py` 把论文在我们科研 harness 上的运行记录写成本目录的格式：可对应的产物经 `WorkflowRunLog` 回放，调用用量写成 JiuwenSwarm 的 token 字段。只接受同一概念的产物，对应关系列在 `ARTIFACT_EQUIVALENCE`。每行用量带 `source_record`，指回原始记录。执行入口是 `scripts/import_research_run.py`。

新课题由 `scripts/run_research_front.py` 从研究问题开始跑完选题、检索、分析；检索与分析步骤在 `research_stages.py`，与事后补跑共用。build 阶段经 `common/research/fulltext.py` 取开放获取全文，analyze 阶段的候选句来自摘要与全文结果段。`scripts/run_research_back.py` 接着同一份运行日志（`WorkflowRunLog.resume()`）跑完后三个阶段，从研究问题到 PDF 全程无人值守：

| 阶段 | 谁来做 | 产物 |
|---|---|---|
| propose | 设计在代码里先于结果固定；Jev 回答审稿人会问的两个问题（设计是否回答得了研究问题、是否存在发表偏倚），设计不合格就停在这一阶段；方法总览图由 `common/research/method_figure.py` 生成（版式卡不含数字，有可见数字的图不用） | study_spec、adversarial_resolution、method_figure |
| experiment | 论文级计票综合：每篇论文按其有方向的发现多数定立场；支持比例做精确二项检验与 Clopper–Pearson 区间，再按发表年份中位数两半各做一次；结果图由 `common/research/result_figures.py` 按学科样式从登记表画出 | experiment_code、experiment_result、verified_registry、result_figures |
| write | `common/research/exemplars.py` 以同类综述为范文规划章节、按学科选写作规范；写作者拿到本次运行实际执行的检索与编码协议（数字进登记表）、每篇论文的编码立场与声明类各节的事实；一个 JiuwenSwarm DeepAgent（`create_deep_agent`）写稿，挂 `RigorAuditRail`、`GovernanceReviewRail`、`UsageLedgerRail`；附录的纳入研究表由代码从记录生成，撤稿与出版方通告不进综合；交付门检查引用键、DOI 注册、数字登记（含图注与附录）、残留（含写作者谈论自身指令的话）、计划章节、图与附录引用、LaTeX 编译，并要求本稿数字核对持有认证；有 finding 退回写作者，最多两轮；通过后 `common/research/review_ensemble.py` 的三位审稿人与领域主席给出只改写的任务，修订稿过门且同一组审稿人复审不低于原稿才采纳 | exemplar_plan、draft_pack、review_report、final_bundle（PDF）、process_summary |

`UsageLedgerRail` 在每次模型调用后读响应自带的用量，逐条记入运行日志；没有用量的响应记为 `measured=false`，不跳过。写作者的回答、方法图/范文/审稿的模型应答（`common/research/gateway.py`）、Jev 回答、DOI 查询都缓存在运行目录，`--replay` 不联网、不需 key，逐字节重现全部 JSON 产物与 LaTeX 源文件。

运行记录里没有的阶段产物由 `scripts/complete_research_run.py` 事后补跑：OpenAlex 取文献元数据，Jev（`common/tools/jev_decision.py`）做判定。补跑产物以 `origin=post_hoc_completion` 进入同一次回放；全部外部请求与应答缓存在输出目录，`--replay` 离线逐字节重现。
