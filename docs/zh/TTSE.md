# TTSE 双轨自演进

TTSE（Two-Track Self-Evolution）从对话轨迹归纳环境事实（FACT）与能力选择提示（TIP），注入系统 prompt。它与 [Skill 正文演进](Skill自演进.md) 相互独立：**不改 SKILL.md，也没有审批弹窗**。

内置 TTSE 由 `react.ttse.enabled` 控制，仅在 agent/work 模式生效。随包模板 **默认关闭**：设 `enabled: true` 后挂载 `TTSERail`（依赖 agent-core 提供该 rail；缺失时 Host 侧降级跳过）。主 code 模式只通过会话中选择 TTSE 插件启用，不再由旧开关注入内置 Rail。

轨迹归纳依赖 LLM/工具 span：开启 TTSE 时 Host 会像 Skill/Symphony 演进一样自动拉起 `agent_observability`（即使其 `enabled: false`），否则 `run_evolution` 因无轨迹而静默跳过。

```yaml
react:
  ttse:
    enabled: false          # 默认关闭；只控制 agent/work 的内置 TTSE
    evolve_enabled: true    # 是否从轨迹归纳 FACT/TIP
    inject_enabled: true    # 是否注入系统 prompt
    # Auto-dream（静默整理经验库，不劫持用户回合）
    dream_enabled: true     # 是否启用 Auto-dream
    consult_top_k: 8        # ttse_consult 每轨（FACT/TIP）返回条数
    consult_retrieve_mode: hybrid  # hybrid | embed | bm25；池子不够仍 dump
    # 语义 dedup / Auto-dream / ttse_consult 混合召回；三段齐全时 BM25+embedding，否则 BM25 兜底
    # 环境变量名与 embed.* 一致，勿硬编码内部端点
    embedding:
      api_key: "${EMBED_API_KEY}"
      base_url: "${EMBED_API_BASE}"
      model: "${EMBED_MODEL}"
```

规则库固定在 agent workspace 下，按模式分库：agent 模式为 `.ttse/work/bank.json`，code 模式为 `.ttse/code/bank.json`（不写入用户项目目录），两库的归纳、检索与 dream 互不相通。注入方式固定为 `disk_catalog`（指引写入系统 prompt，FACT/TIP 正文走 `ttse_consult`），二者都不作为用户配置项。

Auto-dream 的 `dream_interval` / `dream_min_hours` / `dream_ttl_days` 固定为 `50` / `24.0` / `90`，**不对用户开放**。work 内置实例由 Host 构建，code 插件实例由 ZIP 内的 Rail 工厂构建。开启 `dream_enabled` 后会对已有 FACT/TIP bank 做卫生（TTL 剪枝、近重合并、低质量 TIP 清洗），与在线 `induce`/`blame` 独立。

`embedding` 可选；变量名以环境变量为准（`EMBED_API_KEY` / `EMBED_API_BASE` / `EMBED_MODEL`）。`consult_retrieve_mode` 选打分路径（`hybrid` / `embed` / `bm25`）；向量缺失或失败走 BM25，池子不够仍 dump。模型应调用 `ttse_consult(category=…, query=处境短句)`；`category` 与 `query` 都必须填，查全集用 `category=all`。正文不灌进 system。

## 通过插件接入主 Coding Agent

源码位于 `jiuwenswarm/resources/plugins/ttse`，执行 `python scripts/package_ttse_plugin.py` 生成 `dist/plugins/ttse_2.0.0.zip`。导入、安装后在主 code 会话选择；只安装不采集轨迹。

TTSE 2.0.0 使用[原生插件通用运行时](原生插件运行时.md)，metadata.plugin_runtime contract v1。宿主仅提供声明服务及生命周期，不再有 TTSE 专用 SDK/管理器。包内 prepare_plugin_context 负责配置、旧 react.ttse 兼容及 bank 校验；from_plugin_context 负责 TTSEConfig、归纳调用策略和实例构建。

优先级为包内默认 → config.yaml.ttse → 旧 react.ttse 业务选项 → plugins.ttse。轨迹由 requires 声明按租约管理；旧 enabled 不构建 code 内置 Rail，取消插件也不恢复内置实例。work 内置 TTSE 保持独立。

主模型、轨迹、数据根和 embedding_factory 来自通用服务，凭据仅消费宿主配置。卸载等待归纳/dream 真正退出后清理目录、释放租约，保留 bank。超时看护及失败快照恢复由通用管理器执行，不影响其他插件的租约。

旧 TTSE 1.x 的 coding_runtime v1/v2 已替代，需配套升级宿主与 2.0.0 ZIP。core 保留同步文件工厂、部分绑定失败清理及 TTSE 异步关闭，本次通用化不增加 core API。
