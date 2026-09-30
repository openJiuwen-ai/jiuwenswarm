---
name: conception-orchestration
description: 管理科研构思的输入收集、一次性生成、交付和用户主动发起的会话内修改；当用户需要新建、修改或确认构思模块结果时使用。
---

# 构思会话编排

## 目标

- 使用全局 `conception` Skill 生成正式构思结果。
- 由当前 Agent 会话管理用户主动提出的修改，同时保证每次 Skill 调用仍然无状态。
- 最终产物符合 [references/conception-schemas.md](references/conception-schemas.md)，可直接交给规划模块。

## 工作流

### 1. 收集输入

建立以下四组对象：

- `research_request`
- `domain`
- `resource_constraints`
- `search_constraints`

只追问缺失的必填字段。不要擅自估算用户未提供的算力、预算或时间。`search_constraints.max_results` 必须为 10 到 50，以便生成恰好 10 篇参考文献。

`search_constraints.sources` 支持 `arxiv`、`semantic_scholar`、`openalex` 和 `crossref`。`arxiv` 无需 Key；其他来源也可匿名调用，但可以通过运行时 `api_config` 提供 `semantic_scholar_api_key`、`openalex_api_key`、`crossref_email` 或 `crossref_plus_api_token`，也可由启动进程提供对应的大写环境变量。该对象不属于业务接口，敏感值不得出现在展示内容或输出中。

### 2. 生成首版

用四组输入调用全局 `conception` Skill。Skill 每次启动新的原生 `research_agent`，由其调用受限文献工具检索真实记录、进行有限工具迭代并接受程序强校验；它不保存跨调用状态，也不自动重写已交付结果。正式结果必须包含恰好 10 篇 `references`，且 `key_papers` 必须是其子集。Skill 返回 `FAILED` 时，向用户说明具体问题，不把 `partial` 当成正式交付。

### 3. 展示与交付

将 `PASS` 结果中的八个业务字段作为当前版本保存在会话上下文中。向用户展示：

1. 研究问题、假设和研究空白摘要。
2. 参考文献总数及关键论文。
3. 可直接传给规划模块的完整业务 JSON。

### 4. 用户反馈修改

收到反馈后创建以下附加输入，再次调用全局 `conception` Skill：

```json
{
  "revision_context": {
    "previous_output": {},
    "feedback": "用户本轮原始反馈"
  }
}
```

`previous_output` 必须只包含上一版八个业务字段，不得包含 `status`、`errors` 或 `partial`。四组原始输入仍须同时传入；不要依赖 Skill 的历史记忆。每轮只保留最近一次通过校验的完整版本作为下一轮基线。

### 5. 交付

删除运行时状态字段后交付完整业务 JSON，并标记可进入规划模块。交付字段为 `research_question`、`hypotheses`、`gap_report`、`key_papers`、`references`、`research_frontier`、`domain` 和 `resource_constraints`。若用户改变研究方向，应建立新的首版，不沿用旧方向的文献和研究空白。

## 决策规则

- 输入缺必填字段：先补输入，不调用 Skill。
- 文献无法核验或输出校验失败：状态保持 `FAILED`，不得进入规划。
- 文献 API 不可达或真实候选少于 10 篇：状态保持 `FAILED`，调整配置或检索约束后重试。
- 用户只要求解释当前结果：直接解释，不触发新一轮生成。
- 用户明确提出修改：传入完整 `revision_context`，触发新的单次 Skill 调用。
- 用户确认当前版本：冻结业务字段并交付规划模块。

## 验收标准

- Skill 本身不读取会话历史，也不持久化状态。
- Skill 每次调用只执行一次模型生成和程序强校验。
- 每次修订都能从本次显式输入重建所需上下文。
- 正式输出包含六类生成结果和两个透传对象。
- `references` 恰好 10 篇、id 不重复且元数据可核验，`key_papers` 是其子集。
- 研究问题、假设、研究空白和文献相互一致，且满足接口强校验规则。
