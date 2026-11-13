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
  - jiuwenswarm/agents/harness/common/prompt
  - jiuwenswarm/agents/harness/common/rails
  - jiuwenswarm/agents/harness/common/recommendation
  - scripts
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
  - jiuwenswarm/agents/harness/common/prompt/browser_task_prompt.py
  - jiuwenswarm/agents/harness/common/rails/browser_task_prompt_rail.py
  - jiuwenswarm/agents/harness/common/recommendation/topic_search.py
  - jiuwenswarm/agents/harness/common/recommendation/topic_model.py
  - jiuwenswarm/agents/harness/common/recommendation/topic_starter.py
  - scripts/release_dependencies.py
  - scripts/build.sh
  - scripts/build.ps1
  - setup.py
  - pyproject.toml
  - uv.lock
  - MANIFEST.in
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
- `WebResearchBudgetRail` 导入或构造失败时记录警告并跳过该可选 Rail，不能阻止 Work/Code 会话创建；
  其余 Rail 仍照常装配。该降级不表示检索预算已生效，应安装匹配 SDK 后重启。
- Core/Gateway 候选仅用于 editable 开发验证。wheel、sdist 与 PyInstaller 入口校验主依赖、extras、
  uv overrides、uv sources 及已有锁文件，拒绝非官方仓库、浮动修订或同一依赖的不一致固定版本。
  直链依赖先按 URL 结构剥离 query/fragment，再校验官方仓库和完整 SHA；合法的 `#subdirectory=...`
  不属于修订号。该规范化不放宽仓库、SHA 或锁文件一致性要求。
  转 Ready 仅代表进入检视，不解除发布阻断；需先合入对应官方仓、分别统一切换官方完整 SHA、更新锁文件并复测。
- `scripts/build.sh` / `build.ps1` 的非隔离 wheel 构建先用同一个 Python 安装 `build`、`wheel>=0.46.3`、
  `setuptools>=70.1.0` 和 `packaging>=23.0`，安装失败即停止。test extra 与 test group 显式包含
  build-system 依赖；冻结打包的 dev extra 显式包含 `packaging`，不依赖间接安装碰巧满足。
  `setup.py` 的更早期 editable 元数据入口在仅缺少 `setuptools.command.bdist_wheel` 时使用
  `wheel.bdist_wheel`；其他模块导入错误仍抛出。wheel 也在 build-system 中直接声明，避免依赖探测
  尚未结束时先因构建命令导入失败。两条后端路径均保留 wheel/sdist 的官方依赖发布门禁。
- Swarm 不直接固定 Core 的 Git URL，也不配置 SDK uv source/override；SDK 修订唯一由官方
  Gateway 传递。Harmony 仅保留版本下限，claude/codex/cua/distribute 仅声明所需 SDK extras。
  Gateway 更新其 SDK 后，仍须将 Swarm 的 Gateway 固定提交前移并重新生成 uv.lock；
  不会因 Gateway 远端有新提交而自动改变已有锁定环境。
  sdist 包含 uv.lock，保证从源码包构建 wheel 时仍可校验传递 SDK 的官方来源与固定版本，
  锁文件不作为 wheel 的运行时资源。依赖可解析不等于具备配套搜索能力，仍须分别验证。
- 两个默认 rail builder 都包含一份 `WebResearchBudgetRail`；Code 动态配置对该 Rail 去重。
  新 invocation、新并发会话有独立预算；同一 invocation 的模型轮次和并行工具共享。
- 免费来源仅支持 DDG（含 reader 入口）与 Bing；环境模板提供独立开关和检索预算。
  不修改用户真实 `.env`，不自动开启第三方来源或付费服务。SDK 的 provider 参数不能突破来源启用范围。
- 模板提供后台 `FREE_SEARCH_BING_MARKET` / `FREE_SEARCH_BING_FALLBACK_MARKET`，实际选择与低相关回退归 SDK。
  查询语言不决定主市场；主市场扣一次 Bing 额度，同一次搜索内的备用市场不额外扣次数，但仍受剩余时间限制。
  改词要另扣额度，主请求无额度时不能执行备用查询；失败结果不能进入来源台账，不新增前端字段或共享总次数上限。
- Work/Code 的 invocation 预算不覆盖遗留 `mcp_free_search` / 自定义 MCP、浏览器、付费搜索或自定义 Agent Spec；这些仍遵循各自策略。
  无关结果不会伪装成功，但代码不保证源质量、模型最终答案正确或必然结束推理。

## 无搜索配置时的浏览器提示

- Work/默认 Code 的 `BrowserTaskPromptRail` 在每次模型调用前使用 SDK 的
  `is_paid_search_enabled()` / `is_free_search_enabled()` 读取当前配置。
  某个导出缺失时仅替代该可用性判断：免费来源读取 `FREE_SEARCH_DDG_ENABLED` / `FREE_SEARCH_BING_ENABLED`，
  付费来源读取 `BOCHA_API_KEY` / `PERPLEXITY_API_KEY` / `SERPER_API_KEY` / `JINA_API_KEY`。
  判断仍逐轮读取环境，不恢复已移除的 360 来源；已有 SDK 导出保持优先。
  这不替代搜索结果钩子或预算能力，也不承诺旧 SDK 的实际搜索工具可用。
  仅在没有付费搜索 key、DDG/Bing 都关闭且实际挂载 `browser_agent` 时，追加中英文浏览器检索提示；
  配置改变后下一次模型调用移除或重新追加，不缓存初始状态，不影响工具注册与付费/免费优先规则。
- 用户未指定引擎或方式、需要发现网络来源时，提示主 Agent 使用同步
  `task_tool(subagent_type="browser_agent")`，不先试调用未配置或已关闭的搜索来源。
  已知 URL 的正文抓取仍可用 `fetch_webpage`；用户指定方式、站点或禁止浏览器/联网等限制优先。
  浏览器未挂载时不宣传该能力；调用能力不可用时如实说明，不改为 Shell 启动。
- 此提示只描述配置缺失，不将网络错误、验证码、访问限制、低相关或预算耗尽变成浏览器兜底条件，
  不能绕过预算或权限。模型仍负责工具选择；不新增确定性路由或后台话题推荐的浏览器调用。

## 免费来源设置契约

- 设置入口为“智能体 → 网页搜索”；DDG、Bing 是独立布尔开关，均默认关闭。
  `free_search_ddg_enabled` / `free_search_bing_enabled` 映射各自环境变量，使用现有 switch 与中英文文案。
- `config.set` / `config.save_all` 保存到用户 `.env` 并更新运行时环境；配置处理器与控制服务的读取投影必须包含相同字段。
  缺失或空值按 false 返回；读取 `.env` 的已保存值优先于前端服务的旧环境，启动默认值不得覆盖已有 true。
- 两个免费开关的修改均走 search reload scope，沿用既有全局搜索重载通知。
  存量 MCP AbilityManager 根据任意免费来源启用状态添加或移除 `mcp_free_search`，付费工具继续按 API key 独立判断。
- 推荐的搜索能力按 DDG/Bing 任意一种开启判断；支持 duckduckgo、duckduckgo-jina、bing 回执，不要求两个开关同时开启。
  两者均关闭不阻止推荐服务创建、预置话题或热榜展示；`native_search_missing` 仅提示搜索配置缺失，
  不覆盖模型配置状态。实际搜索仍遵循 SDK 开关，不能绕过禁用来源或记录成功回执。
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
浏览器配置兜底验证入口为 `tests/unit_tests/agentserver/test_browser_search_fallback_prompt.py`：
实际 before-model 提示注入、中英文、付费/免费配置矩阵、配置热更新、同步 task 路由及未挂载浏览器边界。
设置与消费方验证入口为 `tests/unit_tests/common/test_free_search_settings.py`、
`tests/unit_tests/gateway/test_app_web_handlers.py`、前端 `tests/settingsRefactor.test.mjs`；
`tests/fixtures/free-search-settings/` 是隔离 RPC 的真实设置组件浏览器验证入口，不替代真实服务端端到端测试。
