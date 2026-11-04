---
id: S_02_web-retrieval
title: SDK Web 检索接入与免费来源配置
type: S
author: ShowHsiang
author_email: xiangxiaoneng@h-partners.com
modules:
  - jiuwenswarm/server/runtime/agent_adapter
  - jiuwenswarm/common/config_panel
  - jiuwenswarm/server/control
  - jiuwenswarm/channels/web/frontend
  - jiuwenswarm/agents/harness/common/tools
  - jiuwenswarm/agents/harness/common/recommendation
files:
  - jiuwenswarm/server/runtime/agent_adapter/trusted_web_search.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_code.py
  - jiuwenswarm/common/utils.py
  - jiuwenswarm/common/config_panel/config_set_handlers.py
  - jiuwenswarm/server/control/config_service.py
  - jiuwenswarm/channels/web/frontend/src/features/settings/services/settingsContract.ts
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/agent/definition.ts
  - jiuwenswarm/agents/harness/common/tools/search_tools.py
  - jiuwenswarm/agents/harness/common/tools/mcp_toolkits.py
  - jiuwenswarm/agents/harness/common/recommendation/topic_search.py
  - jiuwenswarm/agents/harness/common/recommendation/topic_model.py
  - jiuwenswarm/agents/harness/common/recommendation/topic_starter.py
---

# SDK Web 检索接入

## 当前契约

- Work 与默认 Code 的 free_search 使用 `TrustedWebFreeSearchTool`，fetch 使用 SDK `WebFetchWebpageTool`。
  网络、来源选择、质量判断、输出和预算属于 agent-core；应用包装只记录经 SDK 接受的结果 URL。
- 包装调用 SDK `invoke`，透传 `_tool_callback_context`，在 SDK `_on_search_results` 钩子记录来源，
  finally 清理 producer lease。空结果、质量失败、预算失败不能进入可信来源台账；来源可信不等于事实正确。
  SDK 显式保留的 `uncertain` 候选也属于可核验来源，必须原样透传不确定性说明；台账不能将其提升为相关或实时事实。
  SDK 复用回调中当前 Agent 的公共 `get_llm()` 模型一次性评估候选，不保留查询专属别名/意图规则。
  模型随当前调用解析，不缓存到共享工具；原任务由 invocation budget 透传。后台评估超时使用
  `FREE_SEARCH_ASSESSMENT_TIMEOUT_SECONDS=15`，受原总时限约束，不新增模型设置、key 或市场额度。
  `unavailable` 与低相关不同：按 SDK 的 `max_results` 上限透传未评估线索及警告，不成为市场重试条件；
  不显示推荐抓取排名，台账仍只表示发现来源。正常评估中的 uncertain 仍最多两条。
  SDK 的思考档位和输出预算调整只作用于评估调用，不修改主 Agent 模型配置。
  SDK 报告的 PDF/二进制 `unsupported_content`、提取后 WAF `blocked` 原样透传，不能伪装为正文成功。
  备用市场最终接受的来源才记录。不得在包装层重新放回拒绝候选或添加另一套市场重试。
- 必须同时使用含 `_on_search_results` 与 `WebResearchBudgetRail` 的 SDK；旧 SDK 缺钩子时工具返回明确错误，
  不能降级成没有来源记录的“正常”搜索。只更新 Swarm 不足以升级 SDK 行为。
- 两个默认 rail builder 都包含一份 `WebResearchBudgetRail`；Code 动态配置对该 Rail 去重。
  新 invocation、新并发会话有独立预算；同一 invocation 的模型轮次和并行工具共享。
- 免费来源仅支持 DDG（含 reader 入口）与 Bing；环境模板提供独立开关和检索预算。
  不修改用户真实 `.env`，不自动开启第三方来源或付费服务。SDK 的 provider 参数不能突破来源启用范围。
- 模板提供后台 `FREE_SEARCH_BING_MARKET` / `FREE_SEARCH_BING_FALLBACK_MARKET`，实际选择与低相关回退归 SDK。
  查询语言不决定主市场；主市场扣一次 Bing 额度，同一次搜索内的备用市场不额外扣次数，但仍受剩余时间限制。
  改词要另扣额度，主请求无额度时不能执行备用查询；失败结果不能进入来源台账，不新增前端字段或共享总次数上限。
- Work/Code 的 invocation 预算不覆盖遗留 `mcp_free_search` / 自定义 MCP、浏览器、付费搜索或自定义 Agent Spec；这些仍遵循各自策略。
  无关结果不会伪装成功，但代码不保证源质量、模型最终答案正确或必然结束推理。

## 免费来源设置契约

- 设置入口为“智能体 → 网页搜索”；DDG、Bing 是独立布尔开关，均默认关闭。
  `free_search_ddg_enabled` / `free_search_bing_enabled` 映射各自环境变量，使用现有 switch 与中英文文案。
- `config.set` / `config.save_all` 保存到用户 `.env` 并更新运行时环境；配置处理器与控制服务的读取投影必须包含相同字段。
  缺失或空值按 false 返回；读取 `.env` 的已保存值优先于前端服务的旧环境，启动默认值不得覆盖已有 true。
- 两个免费开关的修改均走 search reload scope，沿用既有全局搜索重载通知。
  存量 MCP AbilityManager 根据任意免费来源启用状态添加或移除 `mcp_free_search`，付费工具继续按 API key 独立判断。
- 推荐状态按 DDG/Bing 任意一种开启判断；支持 duckduckgo、duckduckgo-jina、bing 回执，不要求两个开关同时开启。
- 已移除 360 的配置读写、工具分支和推荐回执。旧 `.env` 的 `FREE_SEARCH_360_ENABLED` 不再生效，
  不会单独注册免费工具或开启推荐搜索；SDK 显式 `provider=360` 返回不支持，不偷偷切换引擎。
- 原生推荐的直接搜索不隐式创建模型，保留 SDK 候选的 `unavailable` / `uncertain` 字段与原因；
  遗留 MCP DDG/Bing/fetch 保持各自实现。SDK 私有接口变更时需同步验证原生推荐适配器。
- 前端保存不会自动开启其他来源或付费服务；手工编辑 `.env` 需重启加载。

## 配置与依据

配置见 `docs/zh/配置信息.md` 与 `jiuwenswarm/resources/.env.template` 的 Web 变量。
底层契约唯一来源为 `D:/agent-core/.agents/agent-docs/specs/harness/S_05_tools-contract.md`；不复制 SDK 设计。
验证入口 `tests/unit_tests/agentserver/permissions/test_trusted_web_search.py`：SDK 输出等价、真实质量过滤与来源台账、
预算透传、旧 SDK 兼容错误、Work/Code 实际 builder 装配。
设置与消费方验证入口为 `tests/unit_tests/common/test_free_search_settings.py`、
`tests/unit_tests/gateway/test_app_web_handlers.py`、前端 `tests/settingsRefactor.test.mjs`；
`tests/fixtures/free-search-settings/` 是隔离 RPC 的真实设置组件浏览器验证入口，不替代真实服务端端到端测试。
