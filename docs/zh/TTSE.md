# TTSE 双轨自演进

TTSE（Two-Track Self-Evolution）从对话轨迹归纳环境事实（FACT）与能力选择提示（TIP），注入系统 prompt。它与 [Skill 正文演进](Skill自演进.md) 相互独立：**不改 SKILL.md，也没有审批弹窗**。

由 `react.ttse.enabled` 控制，**仅 agent 模式**生效（code / team 不挂载）。随包模板 **默认关闭**：设 `enabled: true` 后挂载 `TTSERail`（依赖 agent-core 提供该 rail；缺失时 Host 侧降级跳过）。

轨迹归纳依赖 LLM/工具 span：开启 TTSE 时 Host 会像 Skill/Symphony 演进一样自动拉起 `agent_observability`（即使其 `enabled: false`），否则 `run_evolution` 因无轨迹而静默跳过。

```yaml
react:
  ttse:
    enabled: false          # 默认关闭；true 才挂载 TTSERail（仅 agent 模式）
    evolve_enabled: true    # 是否从轨迹归纳 FACT/TIP
    inject_enabled: true    # 是否注入系统 prompt
    # Auto-dream（静默整理经验库，不劫持用户回合）
    dream_enabled: true     # 是否启用 Auto-dream
    # 语义 dedup / Auto-dream / ttse_consult 混合召回；三段齐全时 BM25+embedding，否则 BM25 兜底
    # 环境变量名与 embed.* 一致，勿硬编码内部端点
    embedding:
      api_key: "${EMBED_API_KEY}"
      base_url: "${EMBED_API_BASE}"
      model: "${EMBED_MODEL}"
```

规则库固定在 agent workspace 下的 `.ttse/bank.json`，注入方式固定为 `disk_catalog`（指引写入系统 prompt，FACT/TIP 正文走 `ttse_consult`），二者都不作为用户配置项。

Auto-dream 的 `dream_interval` / `dream_min_hours` / `dream_ttl_days` 由 Host 实现固定为 `50` / `24.0` / `90`，**不对用户开放**。开启 `dream_enabled` 后会对已有 FACT/TIP bank 做卫生（TTL 剪枝、近重合并、低质量 TIP 清洗），与在线 `induce`/`blame` 独立。

`embedding` 可选；变量名以环境变量为准（`EMBED_API_KEY` / `EMBED_API_BASE` / `EMBED_MODEL`）。三段齐全且解析非空时 consult 做 BM25+embedding 混合召回，否则 BM25 兜底。模型应调用 `ttse_consult(category=…, query=经验语义检索句)`；只传 `category` 仍可打开整类。正文不灌进 system。
