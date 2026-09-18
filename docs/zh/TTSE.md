# TTSE 双轨自演进

TTSE（Two-Track Self-Evolution）从对话轨迹归纳环境事实（FACT）与能力选择提示（TIP），注入系统 prompt。它与 [Skill 正文演进](Skill自演进.md) 相互独立：**不改 SKILL.md，也没有审批弹窗**。

由 `react.ttse.enabled` 控制，**仅 agent 模式**生效（code / team 不挂载）。随包模板 **默认开启**：挂载 `TTSERail`，并把 `ttse_consult` 放入首轮 schema（无需 `tools_search`）。设 `enabled: false` 可关闭。

```yaml
react:
  ttse:
    enabled: true           # agent 模式是否挂载 TTSERail
    evolve_enabled: true    # 是否从轨迹归纳 FACT/TIP
    inject_enabled: true    # 是否注入系统 prompt
    # Auto-dream（静默整理经验库，不劫持用户回合）
    dream_enabled: true     # 是否启用 Auto-dream
    # 语义 dedup / Auto-dream / ttse_consult 混合召回；三段齐全时 BM25+embedding，否则 BM25 兜底
    # 环境变量名与 secret_registry embed.* 一致，勿硬编码内部端点
    embedding:
      api_key: "${EMBED_API_KEY}"
      base_url: "${EMBED_API_BASE}"
      model: "${EMBED_MODEL}"
```

规则库固定在 agent workspace 下的 `.ttse/bank.json`，注入方式固定为 `disk_catalog`（P:45 只写指引，FACT/TIP 走 `ttse_consult`），二者都不作为用户配置项。`embedding` 可选；变量名以 `secret_registry` 为准（`EMBED_API_KEY` / `EMBED_API_BASE` / `EMBED_MODEL`）。三段齐全且解析非空时 consult 在类内做 BM25+embedding 混合召回，否则 BM25 兜底。模型应调用 `ttse_consult(category=…, query=经验语义检索句)`；只传 `category` 仍可打开整类。正文不灌进 system。

Auto-dream 对已有 FACT/TIP bank 做卫生（TTL 剪枝、近重合并、低质量 TIP 清洗），与在线 `induce`/`blame` 独立。调度参数由代码固定（每 50 次非 follow-up 迭代尝试一次、距上次成功至少 24 小时、90 天未注入展示则剪枝），仅 `dream_enabled` 可配置。
