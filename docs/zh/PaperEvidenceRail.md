# 论文证据契约与 PaperEvidenceRail

`jiuwenswarm-paper` 运行 agent-core 的论文流水线（`paper_opt.auto_research`）。实验证据能否支撑论文，由主机判定，而不是由模型判定。本页说明这套证据契约，以及在流水线 Agent 内执行该契约的 Harness Rail：`PaperEvidenceRail`。

## 检查对象

| 对象 | 写入方 | 时机 | 内容 |
|---|---|---|---|
| `evidence/protocol.json` | 主机 | 每次执行**之前** | 主指标、必需单元、必需主比较、修订意见要求的比较、预声明题集、各档生效门槛、退休单元、各单元规格哈希、缺失值规则、交付策略、设计身份；ID 为内容哈希 |
| `evidence/manifest.json`（及 `executions/<id>.json`） | 主机审计 | 每次执行之后 | 协议 ID、执行 ID、修订号、各单元的角色（执行 / 复用 / 冻结）/ 状态 / 指标 sha256 / 逐题记录哈希 / 题集覆盖 / 模型 / 数据集 / 预算 / 版本 / 规格、每个必需比较与意见比较的状态、审计结论 |
| `evidence/ledger.json`、`cells/<单元>/v<k>.metrics.json`、`history.jsonl` | 主机审计 | 每次执行之后 | 证据版本台账：每次成功执行都是一个带哈希和单元规格的版本，重跑不覆盖历史 |
| `evidence/retirements.json` | 操作员（`retire`） | 任意时刻，下一个协议生效 | 退休单元：原因、受影响的比较与结论 |
| `acceptance.json` | 主机 | 运行 / 续跑结束 | 流水线完成、证据通过、PDF 检查；另行报告 `primary_hypothesis_verified` |

验收规则只有一条：`evidence.verify()`。执行审计、修订门禁、`acceptance.json` 和 Rail 都调用它，**调用方不传设计**：`verify()` 自己按协议记录的路径（或流水线约定位置 `design/experiment_design.md`）找到并读取当前设计。它每次都从磁盘重新检查：

- 协议未变（旧协议下的审计不计入），协议本身没有问题（`PROTOCOL_INVALID`）；
- 当前设计仍是协议冻结的那份（`DESIGN_CHANGED`）；找不到或读不了时为 `DESIGN_UNVERIFIED`，按未通过处理；
- 每个必需单元均已完成，未被审计错误阻断，且其结果是在当前单元规格下记录的（`CELL_SPEC_CHANGED`）；
- 结果文件哈希与审计时一致（审计后改动即失败）；
- 每个必需主比较均为**已验证**：两边都覆盖同一个预声明题集的全部题目，记录的预算、模型、数据集一致，题目 ID 唯一，主指标完整；没有预声明题集时为 `ITEM_SET_UNDECLARED`；
- 修订中：证据来自本次修订的执行；本次修订执行过的每个单元，要么仍在设计中且有通过的证据，要么有退休记录（`REVISION_CELL_DROPPED`）。

## 结构化实验协议

设计末尾的一个 `experiment-protocol` JSON 代码块，由主机在执行前冻结进协议：

```experiment-protocol
{"item_sets": {"": {"dataset": "hotpotqa-dev", "ids_file": "item_ids.json"}, "m2": {"same_as": ""}},
 "tier_gates": {"t1": 0.5},
 "calibration_cells": ["calib_reference"],
 "cells": {"abl_rank_T1": {"implementation": "v2"}},
 "review_comparisons": [{"item": "R-1a2b3c4d", "a": "proposed_T1", "b": "abl_rank_T1"}],
 "retired": [{"cell": "abl_old_T1", "reason": "...", "affected_comparisons": ["..."], "affected_claims": ["..."]}]}
```

- 有这个块时，设计身份 = 块内容 + 声明的指标名的哈希，**改写正文不会作废证据**；没有这个块时退回全文哈希（任何改动都算设计变更）。
- `item_sets`：每个设置（变体名前缀，`""` 为原设置）预声明的题目 ID（`ids` 或代码目录下的 `ids_file`）。没写时，依次取代码目录的 `item_ids.json`、上一个协议的题集、修订中冻结单元共同的题集。每个变体必须覆盖声明题集；缺整条记录、缺指标、未作答、调用失败、解析失败分别计数；`score_zero` 只对后三类计 0，缺整条记录永远不计分。
- `tier_gates`：各档最低生效率，取值 [0, 1]，进入协议身份；`--tier-gate T1=0.5` 可覆盖。审计**只**读协议门槛，实验输出里的门槛被忽略并报告 `OUTPUT_GATE_IGNORED`。没有预声明门槛时用默认 30%，并作为局限性写明。`calibration_cells` 是定门槛之前的校准运行，不算证据。
- `cells`：单元条目；改动某个单元的条目（如 `implementation`）会让该单元的旧结果失效、需要重跑。

## 结果复用与重跑上限

单元规格 = 名称 / 设置 / 方法 / 档位、主指标与声明指标、缺失值规则、该设置的题集哈希、该档门槛、单元条目、答题模型设置。修订中执行守卫对每个非冻结单元：

1. 台账里有同规格、哈希完好、无阻断错误的版本 → **引用**（结果文件被替换过则从版本副本恢复），不执行，不计执行次数；
2. 只有其他规格的版本 → 写明“规格已变，需要重跑”，旧版本保留；
3. 新增单元上限和单元重跑上限（默认 3 次）**只限制新的执行**：达到上限的单元若有有效版本，照样被引用；
4. 协议中已退休的单元不执行。

比较状态分为 `verified`（已验证）、`failed`（有单元未完成）和 `unverified`（未验证，附原因）。已验证的比较带有结果：`a_better`、`a_worse`、`bounded_null` 或 `inconclusive`。负结果或零效应是证据，可以交付；未验证的比较一律不能解读为“无效应”。

## 配置

| 选项 | 默认 | 含义 |
|---|---|---|
| `--delivery-policy confirmatory\|descriptive` | `confirmatory` | `confirmatory`：所有必需主比较都必须已验证。`descriptive`：可以在缺少验证的情况下交付，但论文不得声称假设已检验，缺口作为必需的局限性写入 |
| `--missing-primary-rule refuse\|score_zero` | `refuse` | 主指标缺失时，该比较记为未验证。`score_zero`：未作答、调用失败、解析失败的题目计 0 分，其他原因的缺失仍拒绝 |
| `--tier-gate T1=0.5`（可重复） | 设计中的门槛，否则 30% | 冻结进协议的各档最低生效率，覆盖设计中的值 |
| `--evidence-rail` | 关 | 挂载 `PaperEvidenceRail`（依赖 rigor 协议，该协议默认开启） |

以上选项连同预算选项都写入 `run_settings.json`；修订期间还会写入 `revision.json`。`resume` 时省略这些选项，就恢复原值；显式给出不同的值，则按新值执行并记为覆盖。

离线命令：

```bash
jiuwenswarm-paper audit    --run-dir runs/p1 [--design .../experiment_design.md]   # 按当前设计重建协议并重新审计（有修订时归入该修订）
jiuwenswarm-paper evidence --run-dir runs/p1                                         # 立即核验；退出码 0 / 2
jiuwenswarm-paper retire   --run-dir runs/p1 --cell abl_x_T1 --reason "..." \
    --affects-comparison "proposed_T1 vs abl_x_T1" --affects-claim "..."             # 退休单元（下一个协议生效）
jiuwenswarm-paper abandon  --run-dir runs/p1 --revision 1 --reason "..."             # 结束修订
jiuwenswarm-paper rollback --run-dir runs/p1 --revision 1                            # 恢复上一稿
```

本契约实施之前的运行没有证据清单。对它们执行 `revise` 之前，需先运行一次 `audit`；否则验收会报告 `NO_EVIDENCE_MANIFEST`。旧运行的设计没有预声明题集时，`audit --item-set <ids 文件>` 可事后声明，但会被记为“执行后由操作员声明”的局限性，不算预注册。

设计在审计后改动：`verify()` 报 `DESIGN_CHANGED`。重新执行（修订中规格未变的单元会被复用）或 `audit` 重建协议；若改动涉及某单元的条件（门槛、题集、单元条目等），该单元的旧结果在新协议下报 `CELL_SPEC_CHANGED`，必须重跑——事后改门槛不能让旧结果过关。

## 修订

修订结束时如果验收未通过，状态变为 `needs_repair`，修订并不关闭。冻结单元、新增单元上限、单元重跑上限（默认 3 次，与新增单元上限分开计）和预算都继续有效。下一次 `resume` 会告诉 manager 还缺什么。只有以下三种情况会结束修订：验收通过（`accepted`）、执行 `rollback`、执行 `abandon`。旧版本自动写入的 `not_accepted` 按 `needs_repair` 处理。

评审意见响应（`revision_response.json`）的结果分为三种：

- `verified_resolved`（已验证解决）：`new_experiment` 条目，且协议的 `review_comparisons` 为该意见 ID 绑定了比较（执行前声明，与原主假设比较分开），每个绑定比较都在声明题集上已验证（结果可以是负的或零效应，不要求显著）、有单元在本次修订中执行、符合 `conditions`；条目自己引用的证据也须通过同样检查，引用退休单元一律不算；
- `addressed`（已落实修改）：改写、缩小结论或承认限制，且位置是 `\label`，或与章节 / 图表标题完全一致——合法，但不算“实验已验证解决”；
- 其余均为未解决：包括只有单边结果的消融、新设置只跑了 proposed 没跑基线、协议没有为该意见绑定比较。

## PaperEvidenceRail

`install_paper_evidence_rail(run_dir)` 包装 `ManagerAgent._create_agent` 和 `ReportingAgent._build_paper_agent`。它们每构建一个 Agent，就通过框架公开接口 `DeepAgent.add_rail` 挂载一次 Rail；重复安装不会叠加。

| 接入点 | 钩子 | 行为 |
|---|---|---|
| 实验执行结束 | 主机执行包装（`execution_audit.AFTER_RECORD_HOOKS`） | 把执行身份与协议身份记入 `evidence/rail_log.jsonl`，并清空缓存 |
| 写作开始前 | reporting 的 `on_user_message` | 在输入前插入证据清单：已验证的比较及其结果、未验证的比较（不得作为结论）、必需的局限性 |
| 请求完成（DONE） | manager 对 `submit_manager_decision` 的 `before_tool_call` | 证据未通过时，借助框架的工具调用跳过机制**跳过**该调用，并把待修任务返回给 manager |

故障处理：检查无法执行（找不到结果、文件不可读、发生异常）时，DONE 按“未验证”拒绝。只有 Rail 自身的日志写入允许失败。Rail 不运行审计，也不调用模型；判定结果按磁盘文件状态缓存，而 DONE 检查一律绕过缓存重新核验。

如实说明的限制：

- 在锁定版本的 agent-core（`9e3390195`）中，实验执行由主机侧 runner 完成，不是 `DeepAgent`，因此不存在“执行结束”的 Rail 回调。这一接入点由现有主机包装承担，它调用的是同一个服务。
- 交付的确定性检查仍是主机的 `acceptance.json` 和修订门禁。Rail 只是更早拒绝 DONE 并说明原因，不能取代它们。如果 manager 反复请求 DONE，最终会耗尽 agent-core 的决策重试次数，而不会进入交付。
- 预算在每轮 manager 开始前、每个实验变体启动前检查。正在运行的变体不会被中断。
- Rail 挂载在 `jiuwenswarm-paper` 路径（`runner.run`）上。AgentServer 的 RSI 论文适配器（`rsi/provider_factory.py`）既不安装 rigor 协议，也不安装本 Rail，保持不变。JiuwenSwarm 的 rail provider 注册机制（`register_rail_provider`）只用于构建 JiuwenSwarm 自身的 Agent，而论文 Agent 是在 agent-core 内部构建的，所以 Rail 采用包装其构建函数的方式挂载。

## 无需模型的示例

```bash
python scripts/paper_evidence_demo.py --out /tmp/evidence-demo
```

示例先按带 `experiment-protocol` 块的设计执行一次实验，再开启一次修订：修订设计为一条评审意见绑定第二模型复现比较，两个新单元一个成功、一个失败，验收结果为 `needs_repair`。“重启”后成功的单元从证据版本**复用**（不执行、不计次数），只重跑失败的单元，Rail 检查与验收随即通过，该意见为 `verified_resolved`。示例中只有实验子进程是替身；执行包装、执行守卫、证据台账、审计、统计、证据清单、验收和 Rail 服务都是真实代码。这是模拟，不是真实论文运行。
