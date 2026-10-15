# Conception Schemas（构思模块对外接口）

> 本文件定义构思模块（模块一）所有**对外接口**的字段契约。
>
> - **10 个对外接口**：4 个上游输入（来自用户 / 配置文件）+ 6 个下游输出（给模块二）。
> - 构思 Skill 内部的 API 候选文献和research_agent 中间结果本次不列。
> - 构思模块内部搜索函数、格式转换函数和持久化函数本次不列。
>
> 设计目标：把用户给出的宽泛研究方向转换为可检索、可验证、可追踪，并可被规划模块直接消费的结构化研究定义。
>
> 下游字段与 `planning-schemas.md` §1 保持一致；`Domain` 和 `ResourceConstraints` 由用户 / 配置文件提供，构思模块只读取和透传，不声明为 Agent 生成结果。

---

## 0. 通用约定

- 模块间正式交换格式统一为 `JSON`；Markdown 只用于人类阅读，不作为下游解析依据。
- 枚举值必须使用接口中声明的值；未知值视为不合法。
- 列表字段必须包含至少一条有效项；不存在时使用显式 `无`，不得用空字符串占位。
- 每个可被交叉引用的列表项必须带唯一 `id`；同一次任务内不得重复。
- 论文、研究现状和研究空白不得凭空生成；相关结论必须能追溯到 `references[].id`。
- 必填项缺一即视为接口不合法（`load_inputs` 失败 / `write_outputs` 拒绝）。
- 构思模块不得替规划模块设计完整方法、数据集、基线和实验矩阵；只提供研究问题、假设、文献依据、研究前沿和研究空白。

---

## 1. 输入侧（用户 / 配置文件 → 模块一）

### 1.1 ResearchRequest（研究请求）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `direction` | str | ✅ | 用户给出的宽泛研究方向，如“Agent 记忆引擎” |
| `problem_context` | str | ⬜ | 研究背景、使用场景或当前痛点 |
| `desired_outcome` | str | ⬜ | 用户期望研究解决的问题或达到的效果 |
| `excluded_scope` | list[str] | ⬜ | 明确不研究的内容，用于控制选题边界 |

来源：用户输入。

### 1.2 Domain（领域标签）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `domain_name` | str | ✅ | 领域名，如 `nlp` / `cv` / `rl` / `agent`；决定文献检索空间和术语体系 |

来源：用户 / 配置文件。

> `Domain` 同时是规划模块输入。构思模块必须原样透传，不得自行改写领域标签。

### 1.3 ResourceConstraints（资源约束）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `gpu_type` | str | ⬜ | GPU 类型，如 A100 / 3090；未指定时由规划模块采用默认值 |
| `gpu_hours` | int | ✅ | 可用 GPU 小时数，必须大于或等于 0 |
| `memory_gb` | int | ✅ | 可用显存 GB，必须大于 0 |
| `budget` | float | ⬜ | 预算，必须大于或等于 0 |
| `time_budget_days` | int | ✅ | 总时间预算（天），必须大于 0 |

来源：用户 / 配置文件。

> `ResourceConstraints` 同时是规划模块输入。构思模块仅用它限制选题规模，并必须原样透传。

### 1.4 SearchConstraints（文献检索约束）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `query` | str | ⬜ | 真实 API 检索词；未提供时使用 `ResearchRequest.direction` |
| `date_from` | str | ⬜ | 检索起始日期，格式 `YYYY-MM-DD` |
| `date_to` | str | ⬜ | 检索结束日期，格式 `YYYY-MM-DD` |
| `languages` | list[str] | ⬜ | 文献语言，如 `en` / `zh`；未指定时默认 `en` |
| `sources` | list[str] | ✅ | 允许使用的真实文献 API，支持 `arxiv` / `semantic_scholar` / `openalex` / `crossref` |
| `max_results` | int | ✅ | 候选文献最大数量，必须为 10 到 50，以保证最终能够筛选出 10 篇有效参考文献 |

来源：用户 / 配置文件。

> `arxiv` 无需 API Key；`semantic_scholar` 和 `openalex` 可匿名调用，也可在运行时 `api_config` 或启动进程环境变量中配置对应 Key；`crossref` 可匿名调用，建议配置联系邮箱，也可配置 Metadata Plus Token。`api_config` 和环境变量都不属于模块间业务接口，不传给research_agent，也不得写进模块输出。

---

## 2. 输出侧（模块一 → 模块二）

### 2.1 ResearchQuestion（主研究问题）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `topic` | str | ✅ | 主题，一句话、动词化，如“用一个轻量化 X 实现 Y” |
| `scope` | str | ✅ | 研究边界：在什么范围内做、明确不做什么 |
| `success_criteria` | str | ✅ | 成功标准：方法可实现且核心假设可通过实验验证 |

生成者：TopicDecomposer。

**发给**：模块二 MethodArchitect / ExperimentPlanner。

### 2.2 Hypothesis（假设，`hypotheses[]` 的元素）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | str | ✅ | 假设唯一 id，格式建议为 `H1` / `H2`；供 MethodDesign.hypothesis_coverage 引用 |
| `claim` | str | ✅ | 可证伪的假设陈述，必须明确干预因素、比较对象和预期变化 |
| `verifiable` | bool | ✅ | 是否可通过实验验证；构思模块最终输出必须为 `true` |

生成者：TopicDecomposer；GapSynthesizer 负责复核可验证性。

**发给**：模块二 MethodArchitect / ExperimentPlanner。

### 2.3 GapReport（研究空白）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `research_question` | str | ✅ | 与 `ResearchQuestion.topic` 对齐的研究问题表述 |
| `existing_state` | str | ✅ | 基于关键论文概括现有方法已经能做到什么 |
| `missing_capability` | str | ✅ | ★ 现有方法缺少的能力；必须足以成为方法设计的切入点 |
| `opportunities` | list[str] | ⬜ | 可由规划模块进一步评估的候选研究方向 |

生成者：GapSynthesizer。

**发给**：模块二 MethodArchitect。

### 2.4 PaperRef（关键论文，`key_papers[]` 的元素）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | str | ✅ | 论文唯一 id；优先使用 arXiv id，其次使用可稳定解析的来源 id |
| `title` | str | ✅ | 论文真实标题，必须与来源记录一致 |
| `source` | str | ✅ | 真实检索来源：`arxiv`、`semantic_scholar`、`openalex` 或 `crossref` |
| `url` | str | ✅ | API 返回或由稳定 id 构造的论文详情页 URL |
| `method_key` | str | ✅ | 核心方法标签，用于方法借鉴、基线选择和重复性检查 |
| `is_baseline` | bool | ⬜ | 是否建议作为候选基线；最终基线由规划模块确认 |

生成者：文献 API 检索脚本提供 id、title、source、url；research_agent 只补充 method_key 和 is_baseline。

**发给**：模块二 MethodArchitect / ExperimentPlanner。

### 2.5 Reference（参考文献，`references[]` 的元素）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | str | ✅ | 参考文献唯一 id；优先使用 DOI 或 arXiv id，其次使用可稳定解析的来源 id |
| `title` | str | ✅ | 论文真实标题，必须与文献 API 返回记录一致 |
| `authors` | list[str] | ✅ | 作者列表，顺序必须与来源记录一致 |
| `year` | int | ✅ | 论文发表年份；预印本使用首次公开年份 |
| `venue` | str | ⬜ | 期刊、会议或预印本平台；来源未提供时显式填写 `无` |
| `source` | str | ✅ | 真实检索来源：`arxiv`、`semantic_scholar`、`openalex` 或 `crossref` |
| `url` | str | ✅ | API 返回或由稳定 id 构造的论文详情页 URL |
| `citation_text` | str | ✅ | 可供人类阅读的完整参考文献条目，格式在同一次任务内必须统一 |
| `relevance` | str | ✅ | 该论文与研究问题、研究现状、研究空白或假设的具体关系 |
| `related_hypothesis_ids` | list[str] | ⬜ | 该论文支撑的假设 id；无直接对应假设时使用空列表 |

生成者：文献 API 检索脚本提供可核验元数据；research_agent 只补充 `citation_text`、`relevance` 和 `related_hypothesis_ids`。

数量要求：正式输出的 `references[]` 必须恰好包含 **10 篇**互不重复、可核验的参考文献。

**发给**：模块二 MethodArchitect / ExperimentPlanner，并保留给后续写作模块生成论文参考文献列表。

### 2.6 ResearchFrontier（研究前沿）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `frontier_text` | str | ✅ | 一段话概括当前主流路线、近期趋势和仍未解决的问题 |

生成者：research_agent 基于本轮 API 候选论文归纳；脚本负责来源一致性复核。

注：该接口与规划模块的弱格式输入保持一致，只检查字段存在且非空，不对段落内部结构做强校验。

**发给**：模块二 MethodArchitect。

> ⚠️ **字段待团队确认**：`planning-schemas.md` 已将 ResearchFrontier 标记为推断字段。若规划模块删除该输入，本接口应同步删除，不得形成仅模块一自用的孤立产物。

---

## 3. 透传接口（模块一 → 模块二）

以下接口不是构思 Agent 的生成结果，但必须由构思模块随正式输出一并传给规划模块：

| 接口 | 处理规则 | 下游用途 |
|---|---|---|
| `Domain` | 原样透传，不得自行改写 | 限定数据集、基线和指标空间 |
| `ResourceConstraints` | 原样透传，不得自行估算或补写用户未提供的预算 | 方法可行性和算力门禁 |

---

## 4. 评审门禁常量

| 常量 | 值 | 说明 |
|---|---|---|
| `CONCEPTION_PASS` | `PASS` | 所有必填字段合法且全部强校验通过 |
| `CONCEPTION_REVISE` | `REVISE` | 用户明确要求修改当前构思，需要返回对应 Agent 重写 |
| `CONCEPTION_FAILED` | `FAILED` | 文献不可验证、输入不足或程序强校验未通过 |
| `MIN_KEY_PAPERS` | 3 | 进入规划模块所需的最少关键论文数 |
| `REQUIRED_REFERENCES` | 10 | 正式交付包必须包含的参考文献数量 |

---

## 5. 强校验规则

1. `ResearchQuestion.topic`、`scope`、`success_criteria` 必须非空，且三者语义不得互相矛盾。
2. `hypotheses[]` 至少包含一条假设，且每条 `verifiable` 必须为 `true`。
3. `hypotheses[].id` 在当前任务内必须唯一，并符合 `H` 加正整数的格式。
4. 每条 `hypotheses[].claim` 必须包含可观察的预期变化，不能只表达愿景或价值判断。
5. `key_papers[]` 数量不得少于 `MIN_KEY_PAPERS`；每篇必须来自本轮真实 API 候选集，且 `id`、`title`、`source`、`url` 必须与候选记录完全一致。
6. `key_papers[].id` 在当前任务内必须唯一；同一论文不得以不同 id 重复出现。
7. `references[]` 数量必须等于 `REQUIRED_REFERENCES`，即恰好 10 篇；每篇必须来自本轮真实 API 候选集，且元数据必须可由来源 URL 核验。
8. `references[].id` 在当前任务内必须唯一；同一论文不得以不同 id、不同版本或不同来源重复计数。
9. `references[].related_hypothesis_ids` 中的每个值都必须指向已存在的 `hypotheses[].id`。
10. `key_papers[]` 必须是 `references[]` 的子集；同一 id 对应的标题、来源和 URL 必须完全一致。
11. `GapReport.existing_state` 和 `missing_capability` 必须基于已核验论文，并使用 `[论文id]` 标注依据，不得凭空生成研究空白。
12. `GapReport.missing_capability` 必须位于 `ResearchQuestion.scope` 内，并能被至少一条假设承接。
13. `ResearchFrontier.frontier_text` 必须非空、使用 `[论文id]` 标注依据，且不得与 `GapReport.existing_state` 明显冲突。
14. `Domain` 与 `ResourceConstraints` 必须和用户输入完全一致；透传过程中不得修改。

---

## 6. 一致性约束

- **GapReport.research_question** 必须与 **ResearchQuestion.topic** 指向同一个研究问题。
- **Hypothesis.claim** 必须位于 **ResearchQuestion.scope** 内，且共同覆盖核心 `missing_capability`。
- **GapReport.existing_state** 与 **ResearchFrontier.frontier_text** 必须由 `references[]` 支撑，不得出现无来源的关键结论。
- **PaperRef.method_key** 必须能解释该论文与现状、空白或候选基线的关系。
- **key_papers[]** 必须从 **references[]** 中筛选，不得引入参考文献列表以外的论文。
- **Reference.relevance** 必须说明论文支撑了哪项现状判断、研究空白或假设，不能只写“与主题相关”。
- **PaperRef.is_baseline = true** 只表示构思模块推荐；规划模块仍需复核数据集、指标和复现条件。
- **ResearchQuestion.success_criteria** 必须能够被规划模块转换为量化的 `ExperimentPlan.success_criteria`。
- **hypotheses[].id** 进入规划模块后必须保持不变，供 `MethodDesign.hypothesis_coverage[].hypothesis_id` 引用。
- **Domain** 和 **ResourceConstraints** 必须与规划模块接收到的对应接口完全一致。

---

## 7. 模块交付包

构思模块成功完成后，统一输出一个 JSON 对象：

```json
{
  "research_question": {
    "topic": "string",
    "scope": "string",
    "success_criteria": "string"
  },
  "hypotheses": [
    {
      "id": "H1",
      "claim": "string",
      "verifiable": true
    }
  ],
  "gap_report": {
    "research_question": "string",
    "existing_state": "string",
    "missing_capability": "string",
    "opportunities": ["string"]
  },
  "key_papers": [
    {
      "id": "string",
      "title": "string",
      "source": "arxiv",
      "url": "https://arxiv.org/abs/...",
      "method_key": "string",
      "is_baseline": false
    }
  ],
  "references": [
    {
      "id": "string",
      "title": "string",
      "authors": ["string"],
      "year": 2026,
      "venue": "string",
      "source": "arxiv",
      "url": "https://arxiv.org/abs/...",
      "citation_text": "string",
      "relevance": "string",
      "related_hypothesis_ids": ["H1"]
    }
  ],
  "research_frontier": {
    "frontier_text": "string"
  },
  "domain": {
    "domain_name": "agent"
  },
  "resource_constraints": {
    "gpu_type": "3090",
    "gpu_hours": 24,
    "memory_gb": 24,
    "budget": 1000,
    "time_budget_days": 7
  }
}
```

> JSON 示例只展示单个数组元素的结构，不代表合法业务值；正式输出的 `references[]` 必须恰好包含 10 个元素，并满足 §5 强校验规则。

---

## 8. 与规划模块的接口映射

| 构思模块产物 | 规划模块输入 | 处理方式 |
|---|---|---|
| `research_question` | `ResearchQuestion` | 直接传递 |
| `hypotheses[]` | `Hypothesis[]` | 直接传递，保持 id 不变 |
| `gap_report` | `GapReport` | 直接传递 |
| `key_papers[]` | `PaperRef[]` | 直接传递 |
| `references[]` | `Reference[]` | 直接传递，共 10 篇；规划模块需预留该输入字段 |
| `research_frontier` | `ResearchFrontier` | 直接传递，字段待团队确认 |
| `domain` | `Domain` | 用户配置原样透传 |
| `resource_constraints` | `ResourceConstraints` | 用户配置原样透传 |
