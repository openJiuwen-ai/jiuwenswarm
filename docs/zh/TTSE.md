# TTSE 双轨自演进

TTSE（Two-Track Self-Evolution）从对话轨迹归纳环境事实（FACT）与能力选择提示（TIP），注入系统 prompt。它与 [Skill 正文演进](Skill自演进.md) 相互独立：**不改 SKILL.md，也没有审批弹窗**。

单 Agent 由 `react.ttse.enabled` 控制，**仅 agent 模式**挂载 `TTSERail`（code 不挂载）。随包模板里该开关 **默认开启**：挂载 `TTSERail`，并把 `ttse_consult` 放入首轮 schema（无需 `tools_search`）。设 `enabled: false` 只关闭单 Agent。

专家团由 `react.ttse.team_enabled` 单独控制，与 `enabled` 解耦。缺省或未写时为 **关闭**；设为 `true` 后团长挂载 `TEAM_TTSE`、成员挂载 `MEMBER_TTSE`。

```yaml
react:
  ttse:
    enabled: true           # 单 Agent；true 才在 agent 模式挂载 TTSERail
    team_enabled: false     # 专家团；与 enabled 独立，默认关闭
    evolve_enabled: true    # 是否从轨迹归纳 FACT/TIP
    inject_enabled: true    # 是否注入系统 prompt
    # Auto-dream（静默整理经验库，不劫持用户回合）
    dream_enabled: true     # 是否启用 Auto-dream
    dream_interval: 50      # 每 N 次非 follow-up 任务迭代尝试一次
    dream_min_hours: 24.0   # 距上次成功 dream 的最短间隔（小时）
    dream_ttl_days: 90      # 超过该天数未注入展示则剪枝
    consult_top_k: 8        # ttse_consult 每轨（FACT/TIP）返回条数
    consult_retrieve_mode: hybrid  # hybrid | embed | bm25；池子不够仍 dump
    # 语义 dedup / Auto-dream / ttse_consult 混合召回；三段齐全时 BM25+embedding，否则 BM25 兜底
    # 环境变量名与 secret_registry embed.* 一致，勿硬编码内部端点
    embedding:
      api_key: "${EMBED_API_KEY}"
      base_url: "${EMBED_API_BASE}"
      model: "${EMBED_MODEL}"
```

规则库固定在 agent workspace 下的 `.ttse/bank.json`，注入方式固定为 `disk_catalog`（P:45 只写指引，FACT/TIP 走 `ttse_consult`），二者都不作为用户配置项。`embedding` 可选；变量名以 `secret_registry` 为准（`EMBED_API_KEY` / `EMBED_API_BASE` / `EMBED_MODEL`）。三段齐全且解析非空时 consult 在类内做 BM25+embedding 混合召回，否则 BM25 兜底。模型应调用 `ttse_consult(category=…, query=处境短句)`；`category` 与 `query` 都必须填，查全集用 `category=all`。正文不灌进 system。

## 主 Agent 查询顺序

TTSE 注入开启且工具可用时，主 Agent 的提示要求：新非闲聊任务的首次工具调用只调用
`ttse_consult`，阅读结果后再加载技能、创建 todo 或调用业务工具；分类不明确时使用 `all`，
目录为空也先查询。无命中或失败后继续，同一任务续跑、审批恢复不重复查询。
闲聊、工具不可用及用户明确要求不查询时除外。这是提示词约束，不提供运行时强制保证，
需要使用包含该指引更新的 agent-core 版本。

## 子 Agent 查询经验

父级已有可用 TTSE 时，general-purpose、research、browser、statusline-setup 和自定义子 Agent
均复用主 Agent 当前的经验库，不在子 Agent workspace 下创建独立经验库。
查询依赖不再按 `agent` 模式名过滤，flash 等非 Code 实例同样可用；父级未挂载 TTSE 时不提供查询能力。
`task_tool` 和持久子 Agent runtime 共用创建入口，自动装配轻量查询 Rail；
工具直接出现在首轮 schema，无需先调用 `tools_search`。

子 Agent 的提示要求：当 `ttse_consult` 可用时，收到新非闲聊任务后，首次工具调用必须仅调用一次
`ttse_consult`，收到并阅读结果后才能加载技能、制定执行计划、创建 todo 或调用其他工具。
即使用户指定技能、目录无匹配分类或为空，也必须先查询；闲聊、工具不可用或用户明确要求不查询时除外。
若缺少目录附件，则明确告知本次未提供目录，并按 `category=all` 查询。
运行时仅提供工具、查询指引和共享经验分类目录，不自动查询或注入经验正文。
查询由模型发起，这是提示词约束，不提供运行时强制保证。
category 按目录选择，无法判断时使用 `all`；query 提取子任务和环境关键词，不整句粘贴任务原文。
无命中或失败后正常继续；同一任务已有结果、工具结果续跑或审批恢复时无需为流程重复查询，
需要更具体的经验时可补查。子 Agent 不启动独立归纳、blame 或 Auto-dream。

沿用 `react.ttse.enabled`、`inject_enabled` 和显式工具禁用配置；自定义 Agent 的
`disallowed_tools: [ttse_consult]` 同样生效。配置关闭后，存活子 Agent 在下一执行边界
移除工具和提示。各子 Agent 独立管理工具资源，底层经验数据共享；Code 实例仍排除，
此查询入口不负责为其他模式启用父级 TTSE。

日志搜索 `[subagent.ttse]`：`consult_start` 表示实际开始调用，`consult_completed`
表示调用完成，`consult_failed` / `consult_cancelled` 表示失败或取消。日志包含子 Agent
类型、子会话 ID、工具 ID 和单次调用 ID，完成日志另含耗时及返回字符数，不记录查询或经验正文。
`source=model` 表示模型发起查询。每次任务结束的 `task_summary` 包含本次执行的
`consult_calls`；为 0 表示本次未调用，不代表同一任务之前没有查询结果。

Auto-dream 对已有 FACT/TIP bank 做卫生（TTL 剪枝、近重合并、低质量 TIP 清洗），与在线 `induce`/`blame` 独立；上述四个 `dream_*` 字段可在配置中覆盖默认值。`consult_retrieve_mode` 选打分路径（`hybrid` / `embed` / `bm25`）；向量缺失或失败走 BM25，池子不够仍 dump。
