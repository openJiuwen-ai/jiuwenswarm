# 办公与开发：两套多 Agent 评测任务

> 2026-09-16 更新：本页保留选型记录。新增两套数据的运行情况见 [SpreadsheetBench / Multi-SWE-bench 复核对照](duplex-review-datasets-20260916.md)；此前结果见[办公与开发实测](duplex-office-code-run.md)和[InterruptBench 更新回放](duplex-interruptbench-replay-20260916.md)。

2026-09-15。按用户最新要求：办公协作 + 写代码、修 Bug、跑测试；两边都需要多 Agent 交互。

**选用 TheAgentCompany 办公任务和 SWE-bench Verified 修复任务。** 本次核对了原始任务、协作方式和评分入口，固定了版本及试跑清单；尚未接入这两套执行器，也没有跑分。

[固定清单](examples/duplex-office-development-selection.json)包含任务 ID、版本、源文件哈希、试跑选择规则和未完成的接入项。

## 现有数据为什么不够

| 现有内容 | 实际覆盖 | 在新方案中的用途 |
| --- | --- | --- |
| `tests/fixtures/duplex/routing_cases.jsonl` | 6 条人工 APPEND / INTERRUPT 判断用例 | 机制回归，不能算办公或开发任务成绩 |
| AgentRadio + SWE-Atlas QnA | 124 个代码库理解题，4 Agent 自然协作 | 保留通信对照；不能代表提交补丁和通过测试 |
| InterruptBench | 网页任务中，用户向一个执行 Agent 补充、修改、撤回需求 | 保留 U2A 回归；不能代替多 Agent 办公协作 |

## 办公：TheAgentCompany

[官方仓库](https://github.com/TheAgentCompany/TheAgentCompany/tree/98b68ef82a47690c316f42fddb05baafaab56851)的固定版本共有 **175 题**。本次按以下规则得到 **21 道办公候选题**：

- 任务属于行政、财务、人事或项目管理（`admin` / `finance` / `hr` / `pm`）。
- 原 `scenarios.json` 至少配置 2 个同事角色。这些角色由独立 LLM 进程扮演，通过 RocketChat 回应。

这 21 题是按工作领域和交互需求筛选的子集，不代表原版 175 题总分。

先用 6 题检查接线，全部沿用原题目、材料、NPC 行为和评分器：

| 原任务 ID | 工作内容 | 原 NPC 数 |
| --- | --- | ---: |
| `admin-get-best-vendor-quote` | 向同事确认服务器要求、取得报价，筛选供应商并提交 CSV 与共享链接 | 2 |
| `admin-collect-requests-and-compute-total-price` | 向四位同事收集设备需求，查价格并汇总成本 | 4 |
| `admin-check-employees-budget-and-reply-and-record` | 核对不同部门预算、回复并记录 | 4 |
| `admin-employee-info-reconciliation` | 联系员工核对资料，修正人员表 | 3 |
| `hr-new-grad-job-description-3` | 向相关同事确认要求，编写岗位说明 | 2 |
| `pm-schedule-meeting-1` | 协调参会时间、安排会议 | 2 |

**Jiuwen 侧计划使用三个执行 Agent：协调、资料处理、复核。** 协调 Agent 保持原题目规定的对外账号，与官方 NPC 沟通；资料 Agent 处理文件和计算；复核 Agent 检查结果，发现遗漏或冲突时发送实际发现。三者走现有 Jiuwen DB 消息链。

原版是“一个被评测 Agent + 多个模拟同事”，不是多个同等执行 Agent 的团队。因此，“三个 Jiuwen 执行 Agent”是我们的协作扩展，需单独记录，不能说成上游自带。NPC 仍使用原配置；其隐藏信息不提供给 Jiuwen 队友。

原 NPC 只回应指定被评测账号；不能直接给每个队友新建 RocketChat 身份，否则收不到原 NPC 回复、评分器也可能找不到会话。对外账号由协调 Agent 使用，内部协作使用 Jiuwen 成员身份；所有 Agent 的实际操作轨迹合并后交原 `/utils/eval.py` 评分。

任务共享的 OwnCloud、RocketChat 等状态需要在每次策略运行前按上游流程初始化。当前只下载并检查了源码，没有启动网站或运行任务。

## 开发：SWE-bench Verified

[官方数据](https://huggingface.co/datasets/princeton-nlp/SWE-bench_Verified/tree/c104f840cc67f8b6eec6f759ebc8b2693d585d4a)已下载并读取：**500 个真实 Issue，覆盖 12 个仓库**。任务输入是 Issue 与固定代码版本，产物是修复补丁；由原 Docker 评测器验证修复测试和回归测试。

**三个 Jiuwen Agent：定位与复现、代码修复、测试与审查。**

1. 定位 Agent 阅读 Issue、复现问题、找到相关代码，将实际证据交给修复 Agent。
2. 修复 Agent 编写补丁；测试 Agent 可以同时准备复现与回归检查。
3. 测试 Agent 在明确的补丁版本上跑测试，把失败日志、遗漏边界或通过结果发回。修复 Agent 根据消息继续或调整当前实现。

源码写入由修复 Agent 负责；测试在该补丁的固定副本上执行，避免运行途中代码被改写。消息应带补丁版本或测试对象，不能把旧版本失败当成新补丁失败。

SWE-bench 本身没有多 Agent 协议，也没有预先写好的中途消息。角色分工和 Jiuwen 消息接入是我们的扩展；消息来自实际诊断、编程、测试，不添加人工“请打断”通知，也不预设正确路由标签。

先固定 **12 题，每个仓库 1 题**：按清单注明的固定哈希规则选择，未看运行成绩。举例：

- `django__django-11532`：非 Unicode 邮件编码下，非 ASCII 域名导致邮件异常。
- `psf__requests-6028`：代理认证问题。
- `pytest-dev__pytest-10356`：获取类标记时考虑方法解析顺序。
- `sympy__sympy-18199`：模方程求根遗漏零根。

试跑用于验证完整链路；正式成绩应覆盖冻结的 500 题，或者明确报告另行冻结子集的结果。

执行 Agent 只拿 `instance_id`、`repo`、`base_commit`、`problem_statement` 及环境版本字段。参考补丁、官方测试补丁、`FAIL_TO_PASS`、`PASS_TO_PASS` 留在评分侧，不能作为测试 Agent 的提前提示。测试 Agent 使用仓库已有测试或自己从 Issue 推导的测试。

## 如何验证全双工收益

本轮按用户要求比较两组，Agent 数量、职责、模型、工具和预算保持一致：

| 组 | 消息处理 |
| --- | --- |
| `steer` | 原 SDK 追加 |
| `model` | 快模型选择 APPEND / INTERRUPT |

主要看原评分器结果、完成时间、所有 Agent 与路由器的总 token、重复工具/测试执行。另记每条消息到达时接收方在生成、执行工具还是空闲，以及实际采取的路由动作。

同题对照不等于消息逐字相同：不同策略可能产生不同探索路径。任务级收益通过同题多次运行比较；固定消息重放只单独用于机制检查。不能为凑 INTERRUPT 数量伪造队友通知。

若多数消息都在 Agent 空闲时到达，这些任务即使完成，也不足以证明双工收益。先用试跑检查“执行中收到有效信息”的覆盖情况；未观察到就如实报告，不强制延时或改写消息冒充自然协作。

## 本次完成与剩余工作

- 已核实并固定两套原始输入；办公 21 题、开发 500 题；试跑分别 6 / 12 题。
- 已生成不含评分答案的 Agent 输入文件，保存在仓库外；版本、哈希和试跑规则保存在固定清单。
- 已增加 `duplex_workloads` 入口：三个 Native Agent，通过 SDK 数据库和 EventBus 自然交换消息；办公独立工作容器，开发分角色副本及带补丁哈希的测试快照。原评分器接入已写入，Docker 执行尚未验证。
- SWE 官方评分器固定为 v3.0.17（`3f01bd622c0a22c00406139f69a234ef08225f22`）；Linux 评分环境已安装。模型服务连通性验证成功。
- 已尝试启动 6 + 12 题、两组共 36 次正式试跑；环境检查阻止执行：本机及已有可达远端缺 Docker，办公服务地址未提供，另一 SSH 主机连接超时。正式任务得分和耗时仍为空；不能据此判断双工收益。

运行命令和验证边界见 [办公开发评测说明](duplex-office-code-run.md)。

本机输入索引：[local-inputs.json](D:/jiusi_agent/_deps/duplex-dataset-review/local-inputs.json)。

## 追加核实的数据集（2026-09-16）

下面把“真实来源”和“真实软件环境”分开标注。研究者设计的任务可以测执行能力，但不能称为真实用户请求。

| 数据集 | 方向 | 已核实事实 | 自动评分 | 对本项目的价值 |
|---|---|---|---|---|
| [SWE-Lancer](https://github.com/openai/preparedness/tree/main/project/swelancer) | 编程 | 论文总集来自 Upwork 的 **1,400+** 个真实付费开发任务；当前 README 说明原 237 题中保留 **198** 道离线修订题。二者统计范围不同 | 独立开发题有端到端测试；经理题为方案选择，应排除出代码修复组 | **补充真实写代码/修 Bug**。选 `ic_swe` 开发题，接定位、修复、测试三个 Jiuwen Agent；无预置异步用户变更 |
| [SWE-bench Pro](https://huggingface.co/datasets/ScaleAI/SWE-bench_Pro) | 编程 | 当前数据卡列出 **731** 个公开测试样本、4 种语言类别；有问题、补充要求、固定 commit 和测试字段 | Docker/Modal 测试，FAIL_TO_PASS / PASS_TO_PASS | 长任务补充；输入经过整理，不等于原始用户对话。公开集条数不能替代私有榜单或历史版本规模 |
| [SWE-Gym](https://github.com/SWE-Gym/SWE-Gym) | 编程 | **2.4K real tasks、11 个 Python 仓库**，带可执行环境和测试验证 | 仓库测试 / verifier | 适合多 Agent 反复“复现→修复→测试”；任务仍以 SWE 类 issue 为主，无自然打断 |
| [Multi-SWE-bench](https://github.com/multi-swe-bench/multi-swe-bench) | 编程 | **1,632** 条多语言 issue，7 种语言，68 名专家从 2,456 候选中筛选 | Docker，官方 harness 输出 resolved/unresolved | 测跨语言开发能力；与 SWE-bench 做分层对照，不与 Python 结果混合 |
| [SpreadsheetBench / Verified](https://github.com/RUCKBReasoning/SpreadsheetBench) | 办公/表格 | **912** 个问题来自在线 Excel 论坛，2,729 个测试用例；已发布专家复核的 **400** 题 Verified 子集 | 多输入用例比较输出工作簿中的目标区域；先重算公式缓存 | **优先用于真实办公请求**；可拆需求解析、表格处理、复核 Agent。问题来源真实，测试工作簿有人工扩展；没有异步变更消息 |
| [SpreadsheetBench 2](https://github.com/RUCKBReasoning/SpreadsheetBench-2) | 办公/财务表格 | **321** 题，真实财报/公司披露材料，专家标注验证；平均 11.8 个工作表、593.5 处单元格修改。论文按生成/调试/可视化分三类，代码拆成四个数据目录 | 非图表题检查目标单元格，并区分应修改与应保留区域；图表另用视觉模型检查 | 适合更长的财务流程；真实材料上的专家任务，不能算原始用户请求。官方运行用 Docker，图表评分要求 Windows/Excel 导图 |
| [OSWorld / OSWorld-Verified](https://github.com/xlang-ai/OSWorld) | 办公/电脑操作 | 当前任务索引 **369** 题，其中 Calc 47、Impress 47、Writer 23、Thunderbird 15、跨应用 101；基于真实用例编制，运行于真实软件 | 每题执行结果脚本；Verified 修订了任务和评分问题 | 适合文档、演示、邮件和跨软件办公；办公子集需独立报告。无预置异步需求更新，也不自带多执行 Agent |
| [WorkArena / WorkArena++](https://github.com/ServiceNow/WorkArena) | 企业办公 | WorkArena-L1 有 33 类任务、19,912 个实例；WorkArena++ 有 682 个组合任务 | BrowserGym/ServiceNow 状态评分 | 适合 ServiceNow 流程和长规划；任务为基准设计，不是企业用户日志，需受限实例访问 |
| [OfficeBench](https://github.com/zlwang-cs/OfficeBench) | 跨应用办公 | 300 个任务：单应用 93、双应用 95、三应用 112；涉及文件、邮件、PDF 等 | Exact/Fuzzy/Execution-based | 适合验证“读表→生成文件→发邮件”链路；来源与真实用户日志性质未充分证明，列为仿真办公 |
| [AppWorld](https://github.com/StonyBrookNLP/appworld) | 应用/API 办公 | 9 个模拟日常应用、457 个 API、约 100 个数字人物；任务可交互执行 | 每题有状态验证和 evaluation 程序 | 适合 API 任务执行；是模拟世界，交互执行不等于自带异步用户修改，优先级低于真实文件和请求 |
| [Terminal-Bench](https://github.com/laude-institute/terminal-bench) | 开发/终端 | 社区构建的终端任务，含编译、调试和环境配置；2.0 通过 Harbor 运行 | 每题测试脚本和参考方案 | 可补开发流程覆盖，但真实终端环境不等于真实用户请求；本次未核实 2.0 任务总数 |

### 推荐顺序

1. **先补两个**：办公选 SpreadsheetBench Verified 400，开发选 Multi-SWE-bench（完整集 1,632；官方另有 flash 300，可降低试跑成本）。沿用现有 SWE-bench 和 TheAgentCompany 记录。
2. **再扩覆盖**：SWE-Lancer 的独立开发题补真实付费修复/功能需求；SpreadsheetBench 2 补长财务流程；OSWorld-Verified 补 Word 类文档、演示和真实桌面操作。
3. **A2A 打断**：保留原题，测试/复核 Agent 依据实际代码、测试日志或工作簿发现问题，发给正在执行的 Agent。消息带补丁或文件版本；是否应打断由冲突决定，不强行增加打断次数。
4. **U2A 用户变更**：继续用 InterruptBench 自身任务及更新链。它的网页更新不能直接拼到无关编程或 Excel 题上；“多轮推理”“真实用户问题”也都不自动意味着带异步打断。

### 现实限制

- 这些公开基准基本都不是多同等 Agent 数据集。多 Agent 角色（协调、执行、测试、复核）和 Jiuwen 消息协议需要我们自己加，原评分器继续作为最终裁判。
- 环境按基准分别记录：SpreadsheetBench 当前评分支持 Linux + LibreOffice 7.5+；公式需重算，不能只用 openpyxl 保存就认定数值有效。SWE-Lancer 官方运行依赖 Linux 离线容器；OSWorld 使用桌面镜像；WorkArena 需申请 ServiceNow 实例。这是官方依赖说明，本轮没有部署或重新探测服务器。
- 若下一步直接在既有服务器执行原测试题，保留原材料和评分逻辑，记录执行器差异；不新建嵌套虚拟机。改变 Agent 组织方式属于我们的实验，使用原评分器也不自动成为官方榜单成绩。
- 两组使用同一任务、Agent 数、模型、工具和预算，记录成功率、总耗时、收到消息后停止过时工作的延迟、实际打断和总调用开销。隐藏答案只留在最终评分侧，复核 Agent 不能靠偷看参考补丁或答案工作簿制造反馈。

### 来源与核实范围

- SpreadsheetBench：上表官方 README、[数据卡](https://huggingface.co/datasets/KAKA22/SpreadsheetBench)、[400 题下载](https://huggingface.co/datasets/KAKA22/SpreadsheetBench/blob/main/spreadsheetbench_verified_400.tar.gz)、[评分代码](https://github.com/RUCKBReasoning/SpreadsheetBench/blob/main/evaluation/evaluation.py)。当前代码以缓存值比较为主，不能把它说成全面验证公式正确性或全部格式。
- SpreadsheetBench 2：[论文摘要](https://arxiv.org/abs/2606.29955)、[数据卡](https://huggingface.co/datasets/KAKA22/SpreadsheetBench-v2)、[评分代码](https://github.com/RUCKBReasoning/SpreadsheetBench-2/blob/main/evaluation/evaluation.py)。
- SWE-Lancer：[论文](https://arxiv.org/abs/2502.12115)核实付费任务来源；当前仓库 README 核实 198 道离线修订题，不能用论文 1,400+ 代替当前可运行规模。
- OSWorld：[论文](https://arxiv.org/abs/2404.07972)、[当前任务索引](https://github.com/xlang-ai/OSWorld/blob/main/evaluation_examples/test_all.json)；本轮按索引实际计数，办公子集不能写成 369 道纯办公题。
- OfficeBench：抽查官方 [3-1/0 原题](https://github.com/zlwang-cs/OfficeBench/blob/main/tasks/3-1/subtasks/0.json)：把项目 Excel 转为 PDF 并形成邮件；评分检查文件和指定内容。任务明确可执行，但这不足以证明来自真实用户日志。
- 其余项目见上表官方仓库/数据卡。本轮核实公开说明和部分配置/评分代码，未下载完整新增数据或实际跑分；规模取核实时公开资料，正式实验另锁定版本和文件哈希。
