# Experiment Schemas（实验模块对外接口）

> 本文件定义实验模块（模块三）所有**对外接口**的字段契约。
>
> - **10个对外接口**：6个上游输入（来自模块二 / 编排层 / 用户配置）+ 4个下游输出（给模块四 / 编排层，或退回模块二）。
> - **5个内部Agent接口**：1个根协调接口 + 4个直属子Agent接口（数据、实现解析、执行、分析）。严格字段和状态流转见[内部Agent契约](internal-agent-contracts.md)。
> - 数据下载、环境检查、代码生成与执行、指标计算、图表生成、资源统计和产物持久化等内部功能函数本次不列。
>
> 6个上游输入为：`MethodDesign`、`ExperimentPlan`、`DataPlan`、`Domain`、`ResourceConstraints`、`ExecutionConfig`。
>
> 4个下游输出为：`ExperimentResults`、`ResourceUsage`、`Reproducibility`、`PlanningFeedback`。
>
> 5个内部Agent接口为：`InitializeResponse`、`DataAgentResponse`、`ImplementationAgentResponse`、`ExecutionAgentResponse`、`AnalysisAgentResponse`。这些接口服务于模块三内部协作，不要求模块二或模块四构造。
>
> 设计目标：把模块二给出的研究方法和实验计划转换为真实、可追溯、可复现，并能被写作模块直接消费的实验交付包。
>
> 机器可读模型：`scripts/contracts.py`。当前状态为接口草案，`contracts.py`已与本草案同步；模块二、模块四确认后再冻结正式版本。

---

## 0. 通用约定

- 传输格式统一为 JSON；Python 中对应 `dict` 或 Pydantic Model。
- 当前接口版本为 `2.0.0`。本版新增领域上下文、分析记录、可视化数据和候选方案；不兼容改动必须升级主版本。
- 所有枚举值使用大写。
- 所有实验必须有稳定且唯一的 `experiment_id`。
- 文件路径均使用相对于本次 `run_dir` 的相对路径，不向队友传递个人电脑绝对路径。
- 缺失值使用 `null`；不得使用“无”“未知”等字符串冒充结构化缺失值。

## 1. 公共函数接口

```python
def run_experiment_module(
    request: ExperimentModuleInput,
) -> ExperimentModuleOutput:
    """执行模块二实验订单，返回模块四可消费的实验交付包。"""
```

JSON调用时，`request`就是本文件第2节定义的完整对象；返回值是第3节定义的完整对象。

## 2. 输入：ExperimentModuleInput

| 参数 | 类型 | 必填 | 来源 | 用途 |
|---|---|---|---|---|
| `schema_version` | str | ✅ | 编排层 | 接口版本，当前为`2.0.0` |
| `run_id` | str | ✅ | 编排层 | 本次科研任务唯一ID |
| `method_design` | MethodDesign | ✅ | 模块二 | 告诉模块三实现什么方法、组件和创新点 |
| `experiment_plan` | ExperimentPlan | ✅ | 模块二 | 告诉模块三运行哪些主实验、基线和消融 |
| `data_plan` | DataPlan | ✅ | 模块二 | 告诉模块三如何下载、预处理和切分数据 |
| `domain` | Domain | ✅ | 模块一，经编排层透传 | 提供领域上下文，用于选择合适的分析方法和候选图表 |
| `resource_constraints` | ResourceConstraints | ✅ | 用户，经模块二透传 | 限制GPU、显存、时间和预算 |
| `execution_config` | ExecutionConfig | ✅ | 编排层/用户配置 | 指定运行目录、随机种子、环境策略、并行度、监控频率、超时、重试和候选图数量 |

### 2.1 MethodDesign

沿用模块二 `planning-schemas.md §2.1`，模块三重点消费：

- `components`：需要实现或组装的组件及其输入输出。
- `innovation_points[].evidence_metric`：创新点用什么指标证明。
- `innovation_points[].experiment_ref`：创新点由哪个实验验证。
- `hypothesis_coverage`：假设、机制与实验ID的映射。
- `implementable`：必须为`true`，否则不开始执行。

### 2.2 ExperimentPlan

沿用模块二 `planning-schemas.md §2.3` 的字段：

- `objectives`
- `datasets`
- `baselines`
- `metrics`
- `primary_experiments`
- `experiment_matrix`
- `ablation_plan`
- `expected_results`
- `success_criteria`
- `compute_estimate`

职责边界：

- 模块二选择数据集、基线、评价指标、实验组合和成功标准，回答“测什么、比较谁、什么结果有研究意义”。
- 模块三负责定位或实现基线、实现指标计算、确定库版本和参数、执行实验并保存结果，回答“怎么跑、怎么算、实际得到什么”。
- `expected_results`只用于执行结束后的对照分析，不能参与指标生成或结果筛选。

#### 2.2.1 指标输入约定

模块二至少提供无歧义的指标名称，如`macro_f1`，不要只写`F1`。模块三根据领域标准或官方实现确定计算方式，并在输出`metric_implementations`中完整记录。

如果指标名称存在会改变结论的歧义，例如`F1`无法判断是`macro`还是`micro`，模块三不得静默选择，应通过`planning_feedback`要求模块二明确评价意图。

#### 2.2.2 experiment_matrix交接约定

模块二当前使用`list[list[str]]`表达实验矩阵。为保证模块三能把创新点和假设映射到实际运行，双方需确认每行至少遵循：

```text
[experiment_id, dataset_name, method_or_baseline, variant, ...]
```

其中首列`experiment_id`必须存在于`primary_experiments`或消融实验约定中，数据集名必须存在于`datasets[].name`。

当前执行器把第二列之后所有不含`=`的非空字符串视为需要比较的实现名称，并对每个实现展开全部seed；`key=value`形式的附加列进入单次运行参数。例如`["exp1", "d1", "baseline", "ours", "batch_size=16"]`会运行`baseline`和`ours`，共同参数为`batch_size=16`。实现名称必须与内部`implementation-manifest.json`的键完全一致。

如果模块二希望避免依赖列顺序，后续可以把每行升级为带字段名的对象；在双方确认前，模块三不得单方面改变模块二Schema。

#### 2.2.3 模块三内部标准化

模块三读取`MethodDesign`、`ExperimentPlan`和`DataPlan`后，在内部生成`ExecutionSpec`，用于将上游设计转换为可执行任务。它是模块三内部结构，不要求模块二生成：

| 字段 | 说明 |
|---|---|
| `experiment_id` | 上游实验ID |
| `experiment_type` | `PRIMARY`或`ABLATION` |
| `hypothesis_ids` | 从`hypothesis_coverage`提取 |
| `dataset` | 从实验矩阵与DatasetSpec提取 |
| `methods` | 主方法、基线和消融变体 |
| `metric_names` | 从ExperimentPlan.metrics提取 |
| `config` | 模块三根据计划、资源和实现确定的实际配置 |

标准化时如果无法可靠建立映射，返回`REPLAN`，不得靠猜测继续执行。

具体方法代码、基线命令和指标定义使用模块三内部[Implementation Manifest](implementation-manifest.md)登记，不要求模块二生成。

### 2.3 DataPlan

沿用模块二 `planning-schemas.md §2.4`，包括：

- `datasets`
- `split_strategy`，必须包含`seed`
- `preprocessing_pipeline`
- `expected_size`

#### 2.3.1 DatasetSpec 数据来源约定

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | str | ✅ | 稳定的数据集名称，也是缺少地址时的检索关键词 |
| `source_url` | URL/null | ⬜ | 模块二已确认的任意领域数据仓库页面或公开下载地址；存在时优先使用 |
| `scale_estimate` | str | ✅ | 预计数据规模 |
| `license` | str/null | ⬜ | 已确认的许可证；名称检索结果也无许可时不会自动下载 |
| `readiness` | enum | ✅ | `available`、`download`或`apply`；`apply`不会自动绕过授权 |
| `preprocess_required` | list[str] | ⬜ | 数据集专属预处理要求 |

DataAgent启用下载权限后按以下顺序处理：

1. `manifest.datasets[name]`指向的本地数据存在且非空时直接使用并计算数据指纹。
2. `source_url`存在时优先解析该地址。普通公开直链直接下载；Hugging Face数据集页和Zenodo记录页先解析为实际文件。若该地址被`_is_dataset_landing_page`判为论文页/仓库落地页/目录页，则不当作数据源，转为按名称检索。
   - **HF 声明文件的存在性校验（2026-09-17 新增）**：`.../datasets/<id>/resolve/<rev>/<file>` 这类地址会先拉该仓库的**真实文件清单**（`/api/datasets/<id>?full=true`，走镜像可达）再决定：逐字符命中→只绑定这一个文件并把版本固定到仓库SHA；**未命中**→在**同一个仓库内**按归一化文件名（去扩展名、去分片后缀、只留字母数字）找唯一匹配的逻辑文件（`conflict_resolution.json` ↔ `Conflict_Resolution-00000-of-00001.parquet`，分片整组纳入）；同名逻辑文件散在多个目录时视为有歧义，**不猜**。采用替代文件名时把理由写进审计产物的`selection_reason`——不静默。
   - 既没命中、也无法唯一对上真实文件名时抛出`DeclaredFileMissing`，**降级为按名称检索**（而不是直接判死），并把真实文件清单写进`provider_errors["declared_source"]`。清单会随`[data]`blocker 回到模块二 planner，供其逐字符修正`source_url`。
3. `source_url=null`时查询Hugging Face与Zenodo，只允许数据集名称的唯一精确匹配，不能把近似名称或热门结果自动当成目标数据。**精确匹配前先剥掉计划名的装饰**（成对括号组、结尾的`split`/`subset`/`part`/`portion`/`sample`词元）：`MemoryAgentBench (conflict-resolution split)`要能匹配到`ai-hyz/MemoryAgentBench`，否则计划名里的split说明会让检索恒返回0条。只删不改——`LongMemEval-Cleaned`不会因此匹配上`LongMemEval`。检索接口**不返回**`siblings`，命中后需补一次`/api/datasets/<id>?full=true`详情查询才能拿到真实文件清单（parquet转换端点没有镜像，直连不通时整条检索会全军覆没）。
4. 搜索候选、仓库请求错误、最终来源、文件URL、许可和版本写入`data/source-resolution/<dataset>.json`。
5. 无结果、多个同名结果、许可不明确或下载失败时返回`REPLAN`。若注册表存在语义兼容候选，只返回其许可、版本、真实文件URL和兼容证据，由模块二重写数据集名称、字段映射、实验矩阵、基线与成功标准后再进入下载；不得在旧契约下静默替换。若有界闭环仍无可用候选，提示把数据放入`execution_config.run_dir/manifest.datasets[name]`后将`readiness`改为`available`，使用相同`run_id`续跑。

联网搜索与下载默认启用，不再设置用户确认点；调用方仍可用`allow_downloads=false`或CLI的`--no-downloads`主动进入离线模式。默认下载不允许绕过`apply`状态、数据许可证、下载大小上限、公共地址校验或安全解压门禁。

#### 2.3.2 审计产物的 `verified` 语义（2026-09-17 新增）

`data/source-resolution/<dataset>.json` 里每个候选带 `verified: bool`：**这个候选的
`download_urls` 是否真的被查证过存在**。`status=RESOLVED` 只说明"解析流程走通了"，
不等于"地址验证过了"——这个区别曾经害过一整轮实验：

- `provider="module2"` 是模块二声明的 URL **原样透传**（模块三有意不联网猜数据是否
  可下载，见 §2.3.1 第2步），所以 `verified=False`；非HF/Zenodo的直链都走这条路。
- 过过注册表检索、HF元数据（`siblings`）或Zenodo接口的候选 `verified=True`。
- 元数据暂时拿不到（离线/镜像故障）时保持透传行为，但同样标 `verified=False`。

**下游义务**：任何把审计候选当成"已验证"再消费的地方，都必须按 `verified` 过滤。
REPLAN 的「已验证候选直链」回喂（模块二 `load_resolved_download_hints`）就栽在这里
——它只看了 `status`，于是 planner 上一轮自己猜的 404 地址被原样端回去、还附赠一句
"已验证"，planner 自然继续照抄同类路径，三轮 REPLAN 全撞在同一个 404 上。旧产物没有
该字段时按 `provider != "module2"` 兜底。

#### 2.3.3 下载源与镜像配置

单一网络端点会让一次抖动摧毁一整轮实验（2026-09实测：`huggingface.co`直连被重置
WinError 10054，同一批`[data]` blocker连烧4轮）。因此网络层集中配置在
`scripts/_network.py`，全部可用环境变量覆盖——**契约不变**：`DatasetSpec.source_url`
仍是单值，多源完全是模块三内部机制，模块二无需感知。

| 环境变量 | 默认值 | 作用 |
|---|---|---|
| `JIUWENSWARM_HF_BASES` | `https://huggingface.co,https://hf-mirror.com` | Hugging Face站点列表，**主源在前**；逗号分隔。默认含国内镜像`hf-mirror.com`做回退；只留官方源即退回单源行为 |
| `JIUWENSWARM_HF_PARQUET_BASES` | `https://datasets-server.huggingface.co` | parquet自动转换端点（`hf-mirror.com`不代理该服务，故默认单端点） |
| `JIUWENSWARM_SEARCH_TIMEOUT_S` | `30` | 检索/元数据请求超时秒数 |
| `JIUWENSWARM_DOWNLOAD_TIMEOUT_S` | `60` | 单个数据文件下载超时秒数 |
| `JIUWENSWARM_DOWNLOAD_RETRIES` | `2` | 单个源的额外重试次数（不含首次） |

回退顺序，任一层成功即停：

1. **源内重试**：同一URL按指数退避重试`JIUWENSWARM_DOWNLOAD_RETRIES`次。只重试瞬时
   故障（连接重置/DNS/超时/5xx/429）；4xx、返回HTML、超出大小上限属确定性失败，立即
   抛出并**不再换源**——换host也是同样结果。
2. **多源回退**：`huggingface.co`域的文件URL按「同路径换host、主源在前」展开候选
   （`_network.download_variants`），逐个尝试。非HF来源（Zenodo、GitHub raw、
   `source_url`直链）原样单源，不为它们凭空造镜像。
3. **解析层名称回退**：文件级全部失败后，仍按§2.3.1第3步按数据集名检索替代来源。

实现边界：

- 审计产物（`data/source-resolution/*.json`）里的`download_urls`**固定用主源**构造，
  保证可复现与可溯源；镜像只在下载层兜底，不会因为「当时镜像活着」就写进审计文件。
- 白名单`_approved_provider_url`放行配置内的HF站点，镜像返回的parquet URL不再被静默丢弃。
- 公网地址校验（`_validate_download_url`）不变：镜像域名是公网域名天然通过，内网地址仍被拒。
- 重试**不**做断点续传（HTTP Range）：需要服务器配合、要处理206/416与已落盘字节记账，
  还要防「服务器忽略Range却返回200全量」导致文件损坏；对「失败就整包重来」的规模不划算。
- 子进程环境白名单`_SAFE_CHILD_ENV_NAMES`已透传上述变量，预处理子进程同样能读到镜像配置。

### 2.4 Domain

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `domain_name` | str | ✅ | 研究领域或任务领域，如`agent`、`computer_vision`、`biomedicine` |

`Domain`不要求模块二重新定义；由编排层把模块一已经确定的领域透传给模块三。模块三结合`domain_name`、`MethodDesign`和`ExperimentPlan`判断适合的统计分析与可视化候选，不可只凭领域名称套固定模板。

### 2.5 ResourceConstraints

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `gpu_type` | str/null | ⬜ | 可用GPU型号 |
| `gpu_hours` | int | ✅ | 最大GPU小时数，必须`>= 0` |
| `memory_gb` | int | ✅ | 最大显存GB，必须`> 0` |
| `budget` | float/null | ⬜ | 费用预算，必须`>= 0` |
| `time_budget_days` | int | ✅ | 总时间预算天数，必须`> 0` |

### 2.6 ExecutionConfig

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `run_dir` | str | ✅ | 相对于可信工作区的本次运行目录；不得使用绝对路径或`..`，所有产物必须位于其中 |
| `seeds` | list[int] | ✅ | 实验随机种子；至少一个 |
| `max_retries` | int | ✅ | 单实验最大重试次数，必须`>= 0` |
| `timeout_seconds` | int/null | ⬜ | 单实验超时；`null`时使用`time_budget_days × 86400`作为有限上限 |
| `max_parallel_runs` | int | ⬜ | CPU/通用子实验最大并发数，默认4，范围1–64；输出顺序仍与冻结任务一致 |
| `max_parallel_gpu_runs` | int | ⬜ | GPU子实验最大并发数，默认1，且不得超过`max_parallel_runs` |
| `monitor_interval_seconds` | float | ⬜ | 实时校验和心跳间隔，默认1秒，范围0.1–60秒 |
| `environment_mode` | enum | ⬜ | `CURRENT`验证当前Python环境；`VENV`在本次`run_dir`部署隔离环境 |
| `allow_dependency_install` | bool | ⬜ | 默认`false`；仅在已审查依赖全部为精确版本时允许安装到本次环境 |
| `min_figure_candidates` | int | ⬜ | 有独立证据角色的候选图目标，默认4；证据不足时不得重复凑图，结果标记`PARTIAL` |
| `max_figure_candidates` | int | ⬜ | 候选图请求上限，默认8；运行时主交付最多保留8幅，全部原始指标仍写入CSV和汇总表 |
| `dry_run` | bool | ✅ | 为`true`时只检查环境、数据和命令映射，不生成科研结论；门禁通过后以`REPLAN`提示改为`false`再正式运行 |

当前内置统计只支持跨seed取均值。若同一主实验和主指标存在多个非基线方法或多个参数配置，模块三不会自动挑选最高/最低值，而是返回`REPLAN`要求模块二明确唯一主结果配置。

### 2.7 输入示例

```json
{
  "schema_version": "2.0.0",
  "run_id": "run_20260823_001",
  "method_design": {
    "research_goal": "验证结构化交接能否提高科研Agent事实一致性",
    "core_mechanism": "用带Schema校验的结构化产物替代自由文本交接",
    "framework": "Schema-Guided Research Swarm",
    "components": [
      {
        "name": "handoff_validator",
        "function": "校验阶段产物",
        "input_schema": "stage output JSON",
        "output_schema": "validated JSON",
        "novelty_degree": "novel"
      }
    ],
    "technical_route": "在阶段边界执行Schema与证据校验",
    "algorithm_reference": [],
    "innovation_points": [
      {
        "claim": "结构化交接提高事实一致性",
        "evidence_metric": "fact_consistency",
        "experiment_ref": "exp1"
      }
    ],
    "hypothesis_coverage": [
      {
        "hypothesis_id": "H1",
        "mechanism": "handoff_validator",
        "experiment_ref": "exp1"
      }
    ],
    "limitations": ["仅验证四阶段科研工作流"],
    "implementable": true
  },
  "experiment_plan": {
    "objectives": ["验证H1"],
    "datasets": [
      {
        "name": "research_tasks_v1",
        "source_url": "https://example.org/research_tasks_v1",
        "scale_estimate": "100 tasks",
        "license": "CC-BY-4.0",
        "readiness": "available",
        "preprocess_required": ["去重"]
      }
    ],
    "baselines": [
      {
        "name": "free_text_handoff",
        "paper_id": "baseline-spec-v1",
        "metric_name": "fact_consistency"
      }
    ],
    "metrics": ["fact_consistency"],
    "primary_experiments": ["exp1"],
    "experiment_matrix": [["exp1", "research_tasks_v1", "free_text_handoff", "schema_handoff"]],
    "ablation_plan": [],
    "expected_results": ["结构化方案优于自由文本方案"],
    "success_criteria": ["fact_consistency提升>=5%"],
    "compute_estimate": 4.0
  },
  "data_plan": {
    "datasets": [
      {
        "name": "research_tasks_v1",
        "source_url": "https://example.org/research_tasks_v1",
        "scale_estimate": "100 tasks",
        "license": "CC-BY-4.0",
        "readiness": "available",
        "preprocess_required": ["去重"]
      }
    ],
    "split_strategy": {"method": "ratio_8_1_1", "train": 0.8, "val": 0.1, "test": 0.1, "seed": 42, "kwargs": {}},
    "preprocessing_pipeline": ["去重"],
    "expected_size": {"rows": 100, "disk_gb": 1.0, "gpu_estimate": 4.0}
  },
  "domain": {
    "domain_name": "agent"
  },
  "resource_constraints": {
    "gpu_type": null,
    "gpu_hours": 8,
    "memory_gb": 16,
    "budget": null,
    "time_budget_days": 2
  },
  "execution_config": {
    "run_dir": "runs/run_20260823_001/03_experiment",
    "seeds": [42, 43, 44],
    "max_retries": 2,
    "timeout_seconds": 3600,
    "dry_run": false
  }
}
```

### 2.8 模块二分散输出兼容边界

`scripts/main.py adapt-planning`读取`method_design.json`、`experiment_plan.json`、`data_plan.json`、`execution_config.json`、`domain.json`和`resource_constraints.json`，补充`schema_version=2.0.0`及基于规范化内容的稳定`run_id`，最终仍必须通过本节正式`ExperimentModuleInput`校验。

- 三列矩阵`[dataset, baseline, variable]`只在数据集/基线可交叉引用，且`variable`能通过实现键或唯一`planning_aliases`确定真实方法、通过唯一主实验或`experiment_ids`确定实验ID时，转换为`[experiment_id, dataset, baseline, method]`。不解析或猜测变量中的参数、消融含义。
- 模块二绝对`run_dir`仅在解析后位于显式可信工作区内时转为相对路径；工作区根目录和工作区外路径均拒绝。只过滤模块二专属`result_dir`，其他未知字段继续触发`extra=forbid`。
- 空或缺失的无预处理声明规范化为`["identity"]`。
- 适配失败返回结构化`REPLAN`，不会产生宽松版输入对象。

## 3. 输出：ExperimentModuleOutput

| 字段 | 类型 | 必填 | 接收方 | 说明 |
|---|---|---|---|---|
| `schema_version` | str | ✅ | 编排层/模块四 | 当前为`2.0.0` |
| `run_id` | str | ✅ | 编排层/模块四 | 与输入完全相同 |
| `status` | enum | ✅ | 编排层 | `PASS` / `PARTIAL` / `REPLAN` / `FAILED` |
| `experiment_results` | ExperimentResults/null | 条件必填 | 模块四 | `PASS`或`PARTIAL`时必须存在 |
| `resource_usage` | ResourceUsage | ✅ | 编排层/资源报告 | 实际耗时、算力、重试统计 |
| `reproducibility` | Reproducibility/null | 条件必填 | 模块四/复现人员 | `PASS`或`PARTIAL`时必须存在 |
| `planning_feedback` | PlanningFeedback/null | 条件必填 | 模块二 | `REPLAN`时必须存在 |
| `warnings` | list[str] | ✅ | 编排层/模块四 | 非阻断问题；没有时为空数组 |
| `errors` | list[str] | ✅ | 编排层 | 阻断错误；没有时为空数组 |

### 3.1 ExperimentResults：给模块四的核心对象

前四个字段保留模块四旧接口需要的兼容投影；其中`tables`和`figures`允许为空，因为最终采用什么图、什么表由模块四根据候选方案决定。

增强交接链为：`MetricRecord`（实验数值）→ `AnalysisRecord`（分析过程）→ `VisualizationDataAsset`（可重绘数据）→ `VisualizationCandidate`（候选设计）→ 模块四选择、组合或重绘。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `key_findings` | list[str] | ✅ | 模块四直接写入Experiments/Conclusion的事实性发现 |
| `tables` | dict[str, str] | ✅ | 兼容字段；模块三已渲染的默认表格正文，没有时为空对象，不代表最终论文选表 |
| `figures` | dict[str, str] | ✅ | 兼容字段；模块三已渲染的默认图像描述，没有时为空对象，不代表最终论文选图 |
| `statistics` | dict[str, float] | ✅ | 主方法在主数据集上的头部指标；键必须来自`ExperimentPlan.metrics` |
| `finding_records` | list[FindingRecord] | ✅ | 每条发现到实验、指标和假设的证据映射 |
| `metric_records` | list[MetricRecord] | ✅ | 多方法、多数据集、多seed的完整指标明细 |
| `experiment_runs` | list[ExperimentRun] | ✅ | 每个实验的状态、配置、日志和指标 |
| `hypothesis_evaluations` | list[HypothesisEvaluation] | ✅ | 对每条假设的证据结论 |
| `metric_implementations` | list[MetricImplementation] | ✅ | 模块三实际采用的指标定义、代码和聚合方式 |
| `baseline_implementations` | list[BaselineImplementation] | ✅ | 模块三实际采用的基线代码来源与版本 |
| `criterion_evaluations` | list[CriterionEvaluation] | ✅ | 对模块二成功标准的实际判断 |
| `analysis_records` | list[AnalysisRecord] | ✅ | 模块三实际执行的领域适配分析及其参数、结果和局限 |
| `visualization_data` | list[VisualizationDataAsset] | ✅ | 模块四可直接读取并重绘的原始/聚合数据；至少含一个`RAW`数据资产 |
| `visualization_candidates` | list[VisualizationCandidate] | ✅ | 候选图或表的设计说明、数据引用、预览和推荐场景；至少一个 |
| `writing_brief_path` | str | ✅ | 结果写作证据摘要，包含证据覆盖、真实发现、完整聚合表、分析限制和图表选用建议 |
| `table_artifacts` | list[TableArtifact] | ✅ | 已渲染的默认表格产物；允许为空数组 |
| `figure_artifacts` | list[FigureArtifact] | ✅ | 已渲染的默认图片产物；允许为空数组 |

#### FindingRecord

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `finding_id` | str | ✅ | 唯一ID，如`F1` |
| `statement` | str | ✅ | 仅描述实际观察，不复述预期结果 |
| `evidence_experiment_ids` | list[str] | ✅ | 支撑该结论的实验ID |
| `metric_names` | list[str] | ✅ | 支撑该结论的指标名 |
| `related_hypothesis_ids` | list[str] | ✅ | 关联假设；无直接关联时可为空数组 |

#### MetricRecord

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `record_id` | str | ✅ | 单条指标记录唯一ID，供`ExperimentRun.metric_record_ids`引用 |
| `experiment_id` | str | ✅ | 对应规划中的实验ID |
| `dataset` | str | ✅ | 对应`ExperimentPlan.datasets[].name` |
| `method` | str | ✅ | 主方法、基线或消融变体名称 |
| `metric` | str | ✅ | 必须来自`ExperimentPlan.metrics` |
| `value` | float | ✅ | 原始数值，不提前格式化百分比 |
| `unit` | str/null | ⬜ | `%`、`ms`、`score`等 |
| `seed` | int/null | ⬜ | 使用的随机种子 |
| `split` | str | ✅ | `train` / `val` / `test` |

#### ExperimentRun

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `experiment_id` | str | ✅ | 对应规划层的实验ID；同一实验可以有多次实际运行 |
| `run_record_id` | str | ✅ | 单次运行唯一ID；区分方法和seed |
| `status` | enum | ✅ | `SUCCESS` / `FAILED` / `SKIPPED` |
| `dataset` | str | ✅ | 数据集名称 |
| `method` | str | ✅ | 方法或变体名称 |
| `command` | str | ✅ | 可复现命令，敏感参数必须脱敏 |
| `config_path` | str | ✅ | 相对`run_dir`的配置路径 |
| `log_path` | str | ✅ | 相对`run_dir`的日志路径 |
| `metric_record_ids` | list[str] | ✅ | 对应指标明细ID |
| `error` | str/null | ⬜ | 失败原因；成功时为`null` |

#### HypothesisEvaluation

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `hypothesis_id` | str | ✅ | 对应模块二`hypothesis_coverage[].hypothesis_id` |
| `verdict` | enum | ✅ | `SUPPORTED` / `NOT_SUPPORTED` / `INCONCLUSIVE` |
| `evidence_experiment_ids` | list[str] | ✅ | 证据实验ID |
| `reason` | str | ✅ | 基于指标的判断理由 |

#### MetricImplementation

模块三根据模块二选择的指标生成，供模块四描述评价方法并保证复现：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | str | ✅ | 必须来自`ExperimentPlan.metrics` |
| `definition_used` | str | ✅ | 本次实验采用的准确计算定义 |
| `implementation` | str | ✅ | 函数、脚本或评测器名称 |
| `parameters` | dict | ✅ | 如`average=macro`；没有时为空对象 |
| `library` | str/null | ⬜ | 使用的库或评测框架 |
| `library_version` | str/null | ⬜ | 库版本 |
| `direction` | enum | ✅ | `MAXIMIZE`或`MINIMIZE` |
| `unit` | str/null | ⬜ | `%`、`ms`、`score`等 |
| `aggregation` | str | ✅ | runner内部的样本级指标算法说明 |
| `seed_aggregation` | enum | ✅ | 跨seed聚合规则；当前仅支持`MEAN` |

#### BaselineImplementation

模块三根据模块二选择的`BaselineSpec`定位、实现并固定可复现版本：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | str | ✅ | 对应`ExperimentPlan.baselines[].name` |
| `paper_id` | str | ✅ | 沿用上游论文来源 |
| `implementation_url` | str/null | ⬜ | 官方代码或可信实现；自主实现时可为`null` |
| `revision` | str/null | ⬜ | 实际使用的发布版本号；无法确认时为`null`并在说明中披露 |
| `entry_command` | str | ✅ | 实际执行入口，敏感参数必须脱敏 |
| `implementation_notes` | str | ✅ | 官方实现、适配实现或自主复现，以及必要改动 |

#### CriterionEvaluation

模块三将模块二的自然语言成功标准规范化并用真实指标判断：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `criterion_id` | str | ✅ | 模块三生成的唯一ID |
| `source_criterion` | str | ✅ | 模块二原始成功标准，不得改写含义 |
| `experiment_id` | str/null | ⬜ | 能无歧义映射时填写实验ID；多个实验无法判断时为`null` |
| `metric` | str/null | ⬜ | 能无歧义识别时填写指标；成功标准未写清指标时为`null` |
| `actual_value` | float/null | ⬜ | 可直接计算时的实际值或差值 |
| `passed` | bool/null | ✅ | `null`表示证据不足，不能判断 |
| `reason` | str | ✅ | 计算过程或无法判断的原因 |

#### AnalysisRecord

模块三根据领域、任务、实验方法和数据特征选择实际分析方法。`method_name`故意不设固定枚举，以允许分类、回归、时间序列、生存分析、消融、定性误差分析等领域特有方法；但采用了什么方法、参数和假设必须显式记录。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `analysis_id` | str | ✅ | 分析记录唯一ID，如`AN1` |
| `method_name` | str | ✅ | 实际分析方法，如`bootstrap_confidence_interval`、`paired_t_test`、`error_taxonomy` |
| `purpose` | str | ✅ | 此分析要回答的实验问题 |
| `source_experiment_ids` | list[str] | ✅ | 分析使用的实验ID |
| `source_metric_record_ids` | list[str] | ✅ | 分析使用的指标记录ID；非指标型定性分析可为空数组 |
| `parameters` | dict | ✅ | 置信水平、检验侧别、分组字段、校正方法等；没有时为空对象 |
| `assumptions` | list[str] | ✅ | 正态性、独立性等分析假设；没有时为空数组 |
| `results` | dict | ✅ | 结构化分析结果，如均值、区间、效应量、p值或错误类别计数 |
| `summary` | str | ✅ | 对真实结果的简短解释 |
| `limitations` | list[str] | ✅ | 该分析的限制；没有时为空数组 |

#### VisualizationDataAsset

这是模块四选择、组合或重绘图表时使用的数据文件描述，而不是把大数组直接塞进JSON。路径指向相对`run_dir`的CSV、JSON或其他开放格式文件。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `data_id` | str | ✅ | 数据资产唯一ID，如`VD1` |
| `path` | str | ✅ | 相对`run_dir`的数据文件路径 |
| `format` | str | ✅ | 文件格式，如`csv`、`json`、`parquet`；不设固定枚举 |
| `data_level` | enum | ✅ | `RAW`或`AGGREGATED` |
| `column_schema` | dict[str, str] | ✅ | 每列/字段名称及数据类型，至少一个字段 |
| `units` | dict[str, str/null] | ✅ | 数值列单位；没有单位时可写`null` |
| `row_count` | int | ✅ | 数据行数，必须`>= 0` |
| `source_experiment_ids` | list[str] | ✅ | 数据来自哪些实验 |
| `source_metric_record_ids` | list[str] | ✅ | 对应指标记录ID；不适用时可为空数组 |
| `aggregation` | str/null | ⬜ | 聚合数据必须说明聚合方式；原始长表通常为`null` |
| `description` | str | ✅ | 数据粒度、字段含义和适用范围 |

这里的`RAW`指未经模块三为某张图汇总的实验级/样本级/seed级绘图源数据，不是要求把原始训练数据集复制给模块四。模块三可以同时提供`AGGREGATED`版本，方便模块四快速复用均值、置信区间等结果。

#### VisualizationCandidate

每个候选表示“可以怎样表达某项实验发现”，并不表示模块四必须采用。`visualization_type`故意使用自由字符串而不是固定枚举，因为图表形式会随领域和任务变化。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `candidate_id` | str | ✅ | 候选唯一ID，如`VC1` |
| `kind` | enum | ✅ | `FIGURE`或`TABLE` |
| `visualization_type` | str | ✅ | 候选形式，如`line_with_ci`、`confusion_matrix`、`qualitative_grid`、`ablation_table`或领域专用名称 |
| `title` | str | ✅ | 候选标题草稿 |
| `purpose` | str | ✅ | 该候选要传达的实验问题或比较关系 |
| `domain_rationale` | str | ✅ | 为什么这种表达适合当前领域、任务和分析结果 |
| `source_data_ids` | list[str] | ✅ | 使用的`VisualizationDataAsset.data_id` |
| `source_experiment_ids` | list[str] | ✅ | 来源实验ID |
| `analysis_ids` | list[str] | ✅ | 支撑候选的`AnalysisRecord.analysis_id` |
| `related_finding_ids` | list[str] | ✅ | 候选要表达的`FindingRecord.finding_id` |
| `design_spec` | dict | ✅ | 可重绘设计，如x/y、分组、颜色、分面、误差线、排序、表格列及聚合规则 |
| `priority` | enum | ✅ | `PRIMARY` / `SUPPORTING` / `OPTIONAL` |
| `recommended_for` | list[str] | ✅ | 建议放置位置，如`main_text`、`appendix`、`supplement` |
| `preview_path` | str/null | ⬜ | 模块三生成的候选预览图/表路径；无法渲染时可为`null` |
| `editable_spec_path` | str/null | ⬜ | 绘图脚本或可编辑规范路径，供模块四重绘 |
| `caption_draft` | str | ✅ | 只陈述真实结果的图注/表注草稿 |
| `limitations` | list[str] | ✅ | 该表达可能造成的误读或适用限制；没有时为空数组 |

候选类型示例仅用于说明，不构成强制清单：分类任务可能产生混淆矩阵或PR曲线，回归任务可能产生残差图，训练过程可能产生带不确定性的学习曲线，消融实验可能产生表格或热力图，视觉任务可能产生定性对比网格，Agent任务可能产生质量—成本—延迟权衡图。默认目标为4幅、主交付最多8幅承担不同证据角色的候选，例如主结果比较、误差或不确定性、机制分析和效率权衡；真实数据不支持某种分析时必须跳过，不能机械重复或伪造信息凑数。方法名过长时允许使用仅限展示的稳定缩写，但`display_label_map`必须逐项保存“缩写 → 完整契约名称”，数据、接口、实验ID和交叉引用始终使用完整名称。

#### TableArtifact

`TableArtifact`只描述模块三已经渲染出的默认预览或兼容产物，不等于最终论文表格。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | str | ✅ | 表格唯一名称，与`tables`中的键对应 |
| `path` | str | ✅ | 相对`run_dir`的CSV、Markdown或LaTeX文件路径 |
| `source_experiment_ids` | list[str] | ✅ | 生成表格所用实验ID |

#### FigureArtifact

`FigureArtifact`只描述模块三已经渲染出的默认预览或兼容产物，不等于最终论文图片。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | str | ✅ | 图片唯一名称，与`figures`中的键对应 |
| `path` | str | ✅ | 相对`run_dir`的图片路径 |
| `caption` | str | ✅ | 供模块四使用的图注草稿，只描述真实结果 |
| `source_experiment_ids` | list[str] | ✅ | 生成图片所用实验ID |
| `displayed_metrics` | list[str] | ⬜ | 图中实际编码的指标；结果图要被模块四直接复用时必须非空 |
| `caption_assertions` | list[str] | ⬜ | 图注允许陈述的可核验语义；缺失时模块四只能重绘，不能直接复用图片 |
| `source_data_ids` | list[str] | ⬜ | 生成图片的数据资产ID，供重绘和溯源 |
| `evidence_role` | str/null | ⬜ | 该图在证据链中的唯一作用，如主比较、稳健性、边界或机制 |
| `display_label_map` | dict[str, str] | ⬜ | 图中缩写/短标签到契约内完整方法名的映射；仅改变显示，不改变方法ID |

同一结果图原则上只声明一个可比较的`source_experiment_ids`作用域。若确需跨实验合图，必须在候选设计中显式声明可比性依据，不能简单把所有实验ID都填入每一幅图。图片缺少`displayed_metrics`或`caption_assertions`时仍可作为运行记录保留，但模块四必须依据原始/聚合数据重绘，不得从文件名或像素猜测语义。

### 3.2 ResourceUsage

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `wall_time_seconds` | float | ✅ | 模块总墙钟时间 |
| `gpu_hours` | float | ✅ | 实际GPU小时 |
| `cpu_hours` | float | ✅ | 实际CPU小时 |
| `peak_memory_gb` | float/null | ⬜ | 峰值内存/显存，以实现约定为准 |
| `llm_prompt_tokens` | int | ✅ | 代码生成/分析使用的输入Token |
| `llm_completion_tokens` | int | ✅ | 代码生成/分析使用的输出Token |
| `estimated_cost` | float/null | ⬜ | 估算费用 |
| `retry_count` | int | ✅ | 总重试次数 |

### 3.3 Reproducibility

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `environment_path` | str | ✅ | 环境说明或锁文件路径 |
| `environment_deployment_path` | str | ✅ | 代码审查后实际部署/验证的环境报告和精确依赖锁定记录 |
| `entry_command` | str | ✅ | 一键复现入口命令 |
| `seeds` | list[int] | ✅ | 实际使用的随机种子 |
| `results_csv_path` | str | ✅ | 完整指标CSV路径 |
| `raw_results_dir` | str | ✅ | 原始结果目录 |
| `logs_dir` | str | ✅ | 日志目录 |
| `execution_monitor_path` | str | ✅ | 实验执行实时状态快照，含排队、运行、成功、失败和实际并行峰值 |
| `execution_events_path` | str | ✅ | 只追加的执行事件流，含启动、心跳、校验、重试、成功、失败与超时 |

### 3.4 PlanningFeedback

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `reason` | str | ✅ | 为什么不能按原计划执行 |
| `affected_experiment_ids` | list[str] | ✅ | 受影响实验ID |
| `blockers` | list[str] | ✅ | 阻断项，如数据不可访问、显存不足、基线不可复现 |
| `suggested_changes` | list[str] | ✅ | 给模块二的修改建议；模块三不得自行篡改原计划 |

每条`blockers`必须带且仅带一个路由前缀，取值与模块二 `load_inputs._FEEDBACK_CATEGORIES`（8 类别）一致：`[data]`、`[compute]`、`[baseline]`、`[method]`、`[metric]`、`[schema]`、`[experiment]`或`[budget]`。后端会在所有`PlanningFeedback`构造时自动补齐前缀，同时保留原阻断文本。

**前缀决定 REPLAN 走哪条路由**，所以归类要落在「谁该修」上：`experiment_plan` 自身字段的问题（消融登记、矩阵覆盖、标识符契约等）用`[experiment]`，模块二会跳过 stage 1、只让 planner 带着反馈重写；误标成`[method]`会把整轮 REPLAN 送去重跑 method-designer，而那条路由**根本改不到 experiment_plan**（2026-09-17 前科：`removed_by` 散文 blocker 因含"实现"字样被判成`[method]`，method-designer 结构化输出重试耗尽 → planning error → 顶层 `aborted_planning_replan`）。

自动分类器对此有一条**锚定字段路径**的前置判定（`_EXPERIMENT_FIELD_MARKERS = ("experiment_plan.", "ablation", "消融")`），先于关键词规则跑——因为关键词是子串匹配，会被 blocker 里引用的字段值抢走（同一批 blocker 里 `…removed_by='…metric_variant 从 full 改为 basic…'` 就因含 "metric" 被判成了`[metric]`）。但**构造 blocker 时显式写前缀最可靠**。

## 4. 状态与交接规则

| 状态 | 条件 | 下一步 |
|---|---|---|
| `PASS` | 所有核心实验形成可信结果，复现信息和证据映射完整 | 交模块四 |
| `PARTIAL` | 至少一个核心实验成功，但部分非核心实验失败或跳过 | 按工作流交模块四，论文必须披露缺失项 |
| `REPLAN` | 数据、方法、预算或实验ID不一致，无法按计划执行 | 退回模块二 |
| `FAILED` | 核心实验均未产生可信结果，且重试已耗尽 | 停止并报告错误 |

重要区别：

- 指标没有达到`success_criteria`，但实验正确完成：仍可为`PASS`，假设判定为`NOT_SUPPORTED`。
- 实验因为代码错误、数据损坏或日志缺失而没有可信结果：不得为`PASS`。

## 5. 强校验与一致性约束

1. `method_design.implementable`必须为`true`。
2. `innovation_points[].experiment_ref`必须存在于`primary_experiments`或实验矩阵首列。
3. `hypothesis_coverage[].experiment_ref`必须能映射到实际实验。
4. `ExperimentPlan.datasets`与`DataPlan.datasets`必须为同一数据集集合。
5. `DataPlan.split_strategy.seed`必须存在。
6. `compute_estimate`超出`resource_constraints.gpu_hours`时不得静默执行，应返回`REPLAN`或由上游明确降档。
7. `metric_records[].metric`、`metric_implementations[].name`和`statistics`的键必须来自`ExperimentPlan.metrics`。
8. 每个`key_findings`必须有对应`finding_records`，且至少引用一个真实成功实验。
9. `tables`、`figures`及候选预览中出现的数字必须能追溯到`metric_records`、`analysis_records`和`visualization_data`；不得只保留图片而丢失绘图源数据。
10. `PASS`/`PARTIAL`必须提供`reproducibility`；`REPLAN`必须提供`planning_feedback`。
11. 失败和跳过的实验必须保留在`experiment_runs`中。
12. 输出中的任何文件路径都必须相对`execution_config.run_dir`。
13. 每个指标必须有且仅有一个`metric_implementations`定义；具体代码、参数和版本由模块三记录。
14. 每个模块二指定的基线都必须有`baseline_implementations`记录；无法获得可信实现时不得伪造，应返回`REPLAN`或明确标记失败。
15. 每条`success_criteria`必须对应一条`criterion_evaluations`；无法无歧义解析时`passed=null`并请求上游确认。
16. `visualization_data`至少包含一个`RAW`资产；每个候选都必须引用有效的数据、分析、发现和实验ID。
17. `visualization_type`和`method_name`不设固定枚举；模块三必须用`domain_rationale`、参数和局限说明选择依据，不能按领域名称机械套图。
18. 聚合方式、不确定性区间、筛选和排序规则必须写入`AnalysisRecord`或`design_spec`，使模块四能够按相同数字重绘。
19. 模块四可以选择、组合、排版和重绘候选，但不得修改源数字或静默改用另一种统计分析；需要改变分析或聚合时应退回模块三重新计算。
20. `implementation-manifest`中方法必须`ready=true`、指标必须`verified=true`且`seed_aggregation=MEAN`；根Agent必须完成隔离的代码审查与冒烟后执行审查，后端再校验Reviewer身份、代码/清单/冒烟SHA-256并写入`execution_approved=true`。LLM不得直接写批准字段；代码、命令、依赖、清单或冒烟结果变化自动失效并回到`CODE_REVIEW_REQUIRED`。
21. 每次实现命令必须实际生成包含计划test指标的JSON/CSV；命令失败、超时、非有限数值或指标缺失不得用`expected_results`补齐。
22. 每个主实验/数据集在执行前必须有唯一主方法和主参数配置；多个候选配置不得靠运行失败后自动选剩余项。
23. 数据集在实验命令执行前后都必须与准备阶段指纹一致；变化时结果判为`FAILED`。
24. `COMPLETED`交付必须提供`outputs/artifact-manifest.json`，覆盖模块四读取的源数据、候选预览、可编辑规格和复现文件。
25. 正式执行前必须存在状态为`READY`的环境部署报告；依赖清单、代码或实现清单变化后审查和部署摘要一并失效。
26. 并行任务必须保持冻结任务一一对应和确定性输出顺序；GPU并发不得超过独立上限。
27. 正式执行必须持续写实时快照与事件流；心跳至少校验配置摘要、日志文件类型和数据指纹，异常时立即终止对应任务并保留失败证据。
28. 每幅候选图必须有独立设计规范和QA文件；候选图按科学问题而非指标笛卡尔积生成，运行时主交付最多8幅。无法达到有效目标时不得复制图形凑数，必须输出`PARTIAL`及短缺说明，全部原始指标仍保留在CSV和汇总表中。

## 6. 给模块四的最小兼容投影

模块四如果暂时不升级接口，可以继续读取以下兼容字段；`tables`或`figures`可能为空：

```json
{
  "experiment_results": {
    "key_findings": ["主方法在主数据集的事实一致性高于基线"],
    "tables": {},
    "figures": {},
    "statistics": {"fact_consistency": 0.91}
  }
}
```

模块四升级后应优先读取：

- `finding_records`：防止结论没有实验支撑。
- `metric_records`：生成准确表格并核对论文数字。
- `hypothesis_evaluations`：决定论文如何表述假设。
- `metric_implementations`：准确撰写评价指标和聚合方式。
- `baseline_implementations`：准确描述基线来源与复现版本。
- `criterion_evaluations`：说明成功标准是否达到。
- `analysis_records`：了解模块三实际采用的领域适配分析、参数和局限。
- `visualization_data`：取得可选择、组合和重绘的原始/聚合数据。
- `visualization_candidates`：比较候选图表的目的、数据来源、设计和推荐位置。
- `figure_artifacts`和`table_artifacts`：可直接采用的默认预览；不合适时按候选规范重绘。
- `reproducibility`和`resource_usage`：生成复现与资源报告。
- `outputs/artifact-manifest.json`：校验上述外部文件的字节数与SHA-256，防止交接后误改。

模块四的标准处理顺序是：先根据论文叙事选择一个或多个`visualization_candidates`，再决定直接使用`preview_path`、组合多个候选，或用`source_data_ids`指向的数据重新绘制。无论采用哪种方式，图表数字都必须保持与模块三的源数据一致。

### 6.1 增强交接示例（仅展示新增字段）

```json
{
  "analysis_records": [
    {
      "analysis_id": "AN1",
      "method_name": "bootstrap_confidence_interval",
      "purpose": "估计不同方法事实一致性的均值与不确定性",
      "source_experiment_ids": ["exp1"],
      "source_metric_record_ids": ["MR1", "MR2"],
      "parameters": {"confidence": 0.95, "resamples": 10000},
      "assumptions": ["不同任务样本相互独立"],
      "results": {"ours_mean": 0.91, "ours_ci": [0.89, 0.93]},
      "summary": "主方法平均事实一致性为0.91，95%区间为[0.89, 0.93]",
      "limitations": ["当前数据集任务类型有限"]
    }
  ],
  "visualization_data": [
    {
      "data_id": "VD1",
      "path": "visualization/fact_consistency_by_seed.csv",
      "format": "csv",
      "data_level": "RAW",
      "column_schema": {"method": "string", "seed": "integer", "fact_consistency": "float"},
      "units": {"fact_consistency": "score"},
      "row_count": 6,
      "source_experiment_ids": ["exp1"],
      "source_metric_record_ids": ["MR1", "MR2"],
      "aggregation": null,
      "description": "每个方法、每个seed一行的绘图源数据"
    }
  ],
  "visualization_candidates": [
    {
      "candidate_id": "VC1",
      "kind": "FIGURE",
      "visualization_type": "point_interval_comparison",
      "title": "不同交接方式的事实一致性",
      "purpose": "同时比较中心趋势和跨seed不确定性",
      "domain_rationale": "Agent评测样本波动明显，区间比只画柱高更完整",
      "source_data_ids": ["VD1"],
      "source_experiment_ids": ["exp1"],
      "analysis_ids": ["AN1"],
      "related_finding_ids": ["F1"],
      "design_spec": {"x": "method", "y": "fact_consistency", "interval": "95% bootstrap CI"},
      "priority": "PRIMARY",
      "recommended_for": ["main_text"],
      "preview_path": "figures/candidates/VC1.png",
      "editable_spec_path": "figures/candidates/VC1.json",
      "caption_draft": "各方法事实一致性的均值与95% bootstrap区间。",
      "limitations": []
    }
  ]
}
```

## 7. 模块边界

| 内容 | 模块二 Planning | 模块三 Experiment |
|---|---|---|
| 数据集 | 选择并说明来源、规模、许可 | 下载、校验、预处理和切分 |
| 基线 | 选择基线并提供论文依据 | 定位/实现代码、固定版本并运行 |
| 指标 | 选择指标、说明评价目标 | 实现计算、固定参数和聚合方式 |
| 分析方法 | 提出要验证的问题和成功标准 | 按领域与数据特征选择分析方法并记录参数、结果和限制 |
| 成功标准 | 提出有研究意义的阈值或比较目标 | 用真实结果判断是否达到 |
| 实验组合 | 设计主实验、基线和消融 | 标准化为ExecutionSpec并执行 |
| 候选图表 | 不固定论文最终图表类型 | 生成领域适配候选、预览、可编辑规范及绘图源数据 |
| 最终图表 | 不负责 | 不决定论文最终采用方案；由模块四选择、组合和重绘 |
| 结果与复现 | 不负责生成 | 保存指标、日志、配置、分析记录和资源消耗 |

模块三负责：数据准备、方法与基线代码实现/生成、指标实现、环境检查、实验运行、领域适配分析、候选图表/表格、绘图源数据、资源统计和复现记录。

模块三不负责：重新定义研究问题、擅自修改方法/成功标准、根据预期挑选结果、决定论文最终图表布局、撰写完整论文或代替模块四解释无证据结论。
