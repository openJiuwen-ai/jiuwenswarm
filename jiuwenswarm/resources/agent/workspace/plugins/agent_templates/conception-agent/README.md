# 科研构思智能体

科研论文自动生成流程的构思模块。它负责把用户给出的宽泛研究方向转化为规划模块能够直接接收的结构化研究定义。

## 核心能力

- 收集研究方向、领域、资源约束和文献检索约束。
- 调用全局 `conception` Skill，由原生 `research_agent` 通过受限工具检索 arXiv、Semantic Scholar、OpenAlex 或 Crossref 的真实记录，再生成研究问题、可验证假设、研究空白、关键论文、恰好 10 篇参考文献和研究前沿。
- 每次 Skill 调用都是新的受限 research-agent 会话；它允许有限工具迭代和程序强校验，但不保留跨调用记忆或自动重写已交付结果。
- 用户主动提出修改时，Agent 把当前版本和反馈显式传入下一次无状态 Skill 调用。
- 按构思模块接口检查结果并交付规划模块。

## 设计边界

- Skill 的生成不保留历史；可选的跨轮人工修改由 Agent 管理。
- 不负责完整方法设计、实验执行和论文写作。
- 不自建 Agent 框架、状态机或模型调用管道。
- `research_agent` 只能依据本轮工具实际返回并经校验的候选记录归纳，不得以会话记忆替代文献证据。
- 正式交付严格遵循全局 `skills/conception/references/conception-schemas.md`：四类业务输入、六类生成输出和两类透传输出。

## API 配置

`arxiv` 无需 Key。`semantic_scholar` 和 `openalex` 可不配置 Key，但配置后通常有更稳定或更高的调用额度；`crossref` 建议配置联系邮箱。运行时对象不会传给 research agent，也不会进入业务输出：

```json
{
  "api_config": {
    "semantic_scholar_api_key": "可选",
    "openalex_api_key": "可选",
    "crossref_email": "可选",
    "crossref_plus_api_token": "可选"
  }
}
```

也可以在启动 JiuwenSwarm 前设置同名大写环境变量：`SEMANTIC_SCHOLAR_API_KEY`、`OPENALEX_API_KEY`、`CROSSREF_EMAIL`、`CROSSREF_PLUS_API_TOKEN`。
