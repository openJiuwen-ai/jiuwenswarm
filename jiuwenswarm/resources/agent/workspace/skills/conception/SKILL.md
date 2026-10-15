---
name: conception
description: |
  一次性科研构思工作流，通过多来源真实文献 API 和 Jiuwen 原生 research_agent 生成研究问题、假设、研究空白、关键论文、10 篇参考文献和研究前沿。
  Use when 用户给出研究方向、领域、资源约束和检索约束，需要为规划模块准备结构化输入，或携带显式 revision_context 重写已有构思。
  Do NOT use for会话记忆、人工审核、方法规划、实验执行或论文写作。
version: "0.6"
kind: swarm-skill
roles: []
---

# Conception（科研构思模块）

本 Skill 是构思模块的一次性执行入口。完整字段契约以 [references/conception-schemas.md](references/conception-schemas.md) 为准。脚本启动一个原生 Jiuwen `research_agent`，由它调用文献工具、调整检索词并基于真实 API 记录生成结果。它使用 SDK 的 `create_research_agent()`，也不读取或保留其他运行的会话历史。

## 输入

业务输入必须包含四个对象：

- `research_request`：必填 `direction`。
- `domain`：必填 `domain_name`。
- `resource_constraints`：必填 `gpu_hours`、`memory_gb`、`time_budget_days`。
- `search_constraints`：必填 `sources` 和 `max_results`；`max_results` 必须为 10 到 50。

人工反馈修订可额外传入 `revision_context`；各文献服务的可选 Key 或联系邮箱通过运行时 `api_config` 传入。这两个对象都不属于模块间业务接口。

## 执行流程

1. 接收 `research_request`、`domain`、`resource_constraints` 和 `search_constraints`；可用 `search_constraints.query` 单独指定检索词，否则使用 `research_request.direction`。
2. 若是人工反馈后的修改，额外接收显式的 `revision_context`，其中包含上一版输出和本轮反馈。
3. 运行 [scripts/workflow.py](scripts/workflow.py)，按 `sources` 调用 `arxiv`、`semantic_scholar`、`openalex` 或 `crossref` 获取真实候选论文。
4. research_agent 可在预算内补充检索；最终真实候选不足 10 篇时返回 `FAILED`。
5. research_agent 只能从工具实际返回的候选记录中生成恰好 10 篇 `references`，再从中选择至少 3 篇 `key_papers`。
6. 脚本复核参考文献元数据、引用 id、假设关联和 `key_papers` 子集关系。
7. 程序删除误放入 `gap_report` 的重复 `frontier_text`；若研究前沿的某个句子引用了未入选 `references` 的论文，则删除该句，不改变已选择的参考文献。
8. 通过程序强校验后直接返回六类生成输出，并原样透传 `domain` 和 `resource_constraints`；所有证据引用必须来自最终 `references`。

用户主动提出的跨轮修改由外层 `conception-agent` 会话管理；本 Skill 只处理当前调用收到的数据。

## 输出

成功结果的业务字段为：

- `research_question`
- `hypotheses`
- `gap_report`
- `key_papers`
- `references`：恰好 10 篇
- `research_frontier`
- `domain`：原样透传
- `resource_constraints`：原样透传

运行结果额外包含 `status`。只有 `status = PASS` 的完整结果才能进入 Agent 会话；`FAILED` 的 `partial` 只用于定位程序校验问题。

## API 配置

- `arxiv`：无需 API Key。
- `semantic_scholar`：可不配置 Key；配置后通常有更稳定的独立限额。
- `openalex`：可不配置 Key；配置免费 Key 后可获得更高的调用额度。
- `crossref`：可直接使用；建议配置联系邮箱进入 polite pool，也可配置 Metadata Plus Token。

调用 Skill 时可以附加仅供运行时使用的 `api_config`：

```json
{
  "api_config": {
    "semantic_scholar_api_key": "可选",
    "openalex_api_key": "可选",
    "crossref_email": "可选的联系邮箱",
    "crossref_plus_api_token": "可选"
  }
}
```

`api_config` 是运行配置，不属于模块间业务接口。也可以在启动 JiuwenSwarm 前设置 `SEMANTIC_SCHOLAR_API_KEY`、`OPENALEX_API_KEY`、`CROSSREF_EMAIL` 或 `CROSSREF_PLUS_API_TOKEN` 环境变量。Key 只发送给对应 API，在交给 research_agent 前会被移除，也不会出现在输出或日志中。

## Files

| File | What it contains | When to read |
|---|---|---|
| [references/conception-schemas.md](references/conception-schemas.md) | 四类输入、六类生成输出、两类透传输出及强校验规则 | 组装输入、检查输出或修改接口时 |
| [scripts/workflow.py](scripts/workflow.py) | 文献 API 客户端、research_agent 编排和输入输出校验 | 运行构思模块时 |

## ResearchAgent 运行约束

使用 SDK `create_research_agent()`，每次调用新建实例。最多 8 次自主检索，另有 2 次研究问题查重、2 次一层引用扩展、3 篇公开原文读取；最多 28 次 ReAct 迭代，总超时 900 秒。不启用文件或 Shell 工具。接入实现位于 `scripts/research_runner.py`。模型必须支持工具调用；加载失败、证据不足或输出不合法时返回 FAILED，不降级为模拟结果。

Agent 自主选择检索词和输入允许的来源组合。`max_results` 限制入选候选池，不阻止继续搜索；新发现先留存，再用 `select_candidates` 替换低相关记录。引用扩展仅一层，公开原文仅访问支持的学术站点，读取失败或截断须明确说明。确定问题后必须用 `check_novelty` 搜索最接近工作，随后重新筛选候选；最终 topic 必须与已查重 topic 一致。仍只输出原契约字段、恰好 10 篇 references，不增加模块二接口字段。

正式结果固定包含 10 篇可核验参考文献；下游字段、类型和来源枚举以正式 schema 为准。
