---
id: F_20261103_1124_sdk-web-retrieval
title: 接入 SDK Web 质量控制与检索预算
type: F
author: ShowHsiang
author_email: xiangxiaoneng@h-partners.com
modules:
  - jiuwenswarm/server/runtime/agent_adapter
  - jiuwenswarm/resources
  - jiuwenswarm/common/config_panel
  - jiuwenswarm/server/control
  - jiuwenswarm/agents/harness/common/tools
  - jiuwenswarm/agents/harness/common/recommendation
  - jiuwenswarm/channels/web/frontend
  - jiuwenswarm/agents/harness/common/rails
  - scripts
files:
  - jiuwenswarm/server/runtime/agent_adapter/trusted_web_search.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_code.py
  - jiuwenswarm/resources/.env.template
  - tests/unit_tests/agentserver/permissions/test_trusted_web_search.py
  - tests/unit_tests/agentserver/permissions/test_deep_required_composition.py
  - tests/unit_tests/agentserver/permissions/test_permission_cold_build.py
  - jiuwenswarm/common/utils.py
  - jiuwenswarm/common/config_panel/config_set_handlers.py
  - jiuwenswarm/server/control/config_service.py
  - jiuwenswarm/agents/harness/common/tools/search_tools.py
  - jiuwenswarm/agents/harness/common/tools/mcp_toolkits.py
  - jiuwenswarm/agents/harness/common/recommendation/topic_search.py
  - jiuwenswarm/agents/harness/common/recommendation/topic_model.py
  - jiuwenswarm/agents/harness/common/recommendation/topic_starter.py
  - jiuwenswarm/channels/web/frontend/src/features/settings/services/settingsContract.ts
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/agent/definition.ts
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json
  - jiuwenswarm/channels/web/frontend/tests/settingsRefactor.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/index.html
  - jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/main.tsx
  - jiuwenswarm/channels/web/frontend/tests/fixtures/free-search-settings/vite.config.ts
  - tests/unit_tests/common/test_free_search_settings.py
  - tests/unit_tests/gateway/test_app_web_handlers.py
  - docs/en/Configuration.md
  - docs/zh/配置信息.md
  - jiuwenswarm/agents/harness/common/rails/browser_task_prompt_rail.py
  - scripts/release_dependencies.py
  - scripts/build.sh
  - scripts/build.ps1
  - tests/unit_tests/agentserver/test_browser_search_fallback_prompt.py
  - tests/unit_tests/test_release_dependencies.py
  - pyproject.toml
  - uv.lock
  - MANIFEST.in
  - Makefile
designs: []
design_note: 用户在对话中确认实际搜索案例分析后的调整策略，无独立设计文档。
---

# SDK Web 质量与预算接入

## 根因和实际路径

候选基于本地 Swarm `8a551009cf`，依赖已有 editable `D:/agent-core`；首轮未修改 pyproject/uv.lock，后续锁同步见召回跟进。
用户五次实际搜索任务显示错误候选、失败抓取和多轮改写共同延长链路。
Work/默认 Code 使用 SDK Web 工具，但 `TrustedWebFreeSearchTool` 复制了 SDK 的请求和渲染逻辑，
所以仅修改 SDK `invoke` 不足以影响实际 Swarm 调用。这是本次必须同步修正的消费方边界。

## 变更与取舍

- 包装改为调用 `super().invoke` 并透传上下文，只通过 SDK `_on_search_results` 记录已通过质量过滤的 URL。
  保留失败/空结果清理及渲染错误发生前的来源记录语义，拒绝不支持新钩子的旧 SDK。
- Work 与默认 Code 的既有 builder 加入预算 Rail，Code 保持动态 Rail 去重；不自建另一套网络工具或全局请求计数。
- 首轮仅配置模板提供 `FREE_SEARCH_360_ENABLED=false` 和默认每源 2 次、最多 2 源、3 fetch、90 秒上限。
  不操作真实配置、用户日志或当前服务；未加入付费回退或隐式浏览器回退。
- 中英文产品文档说明来源切换、内容状态、可用性与事实性的区别，以及配套 SDK 和重启要求。

当前应用契约见 `S_02_web-retrieval`。底层实现与验证记忆在
`D:/agent-core/.agents/agent-docs/features/F_20261103_1124_web-retrieval-quality.md`。

## 验证和交接

测试使用现有虚拟环境和 uv.lock 匹配的缓存 protocol 路径，运行目录与日志隔离到仓库 tmp。
应用测试从真实 SDK invoke 进入，断言模型可见返回文本以及来源台账这一独立副作用；另外验证实际两个 builder 都只装配一份预算 Rail。
最终定向回归 287 项通过，覆盖可信搜索、工具归属、Code spec reload、必需 Rail 装配、权限冷构建和 session 观测快速路径。
初轮两项装配测试的假设未考虑既有推荐/观测 Rail，改为只统计预算 Rail，未删除既有 Rails。
扩大回归发现两个既有测试按字符串比较 Windows 路径大小写，改为 `Path` 比较，未修改生产路径绑定。
运行命令为 `python -m pytest` 加以下测试文件，参数为
`-q -o addopts= -o log_cli=false -o asyncio_mode=auto -p no:cacheprovider --tb=short --show-capture=no`，
并使用仓库 `tmp/` 内新建的 `--basetemp` 避免沙箱默认临时目录的文件替换限制：

- `tests/unit_tests/agentserver/permissions/test_trusted_web_search.py`
- `tests/unit_tests/agentserver/test_deep_adapter_tool_ownership.py`
- `tests/unit_tests/agentserver/test_code_spec_reload.py`
- `tests/unit_tests/agentserver/permissions/test_deep_required_composition.py`
- `tests/unit_tests/agentserver/permissions/test_permission_cold_build.py`
- `tests/unit_tests/agentserver/permissions/test_session_observation_fast_path.py`

运行前激活 `.venv`，`PYTHONPATH` 使用仓库和 uv.lock 所指 protocol 提交 `9ec0370` 的本机缓存，
未将现有环境内较旧的 protocol 安装状态表述为已经修复。SDK 配套回归 139 项通过，详见 SDK 实施记录。
未运行全仓测试、格式化或静态检查，未执行真实模型/桌面端端到端回归。
没有提交、推送或发布。需要同时更新 SDK 与 Swarm，重启后再用原任务核对实际工具次数、总耗时和答案质量。

## 360 设置链路补齐（2026-11-03）

用户随后要求完整适配 360 前端配置。本次沿用已确认的可选来源策略，不自动启用来源、不修改真实 `.env`。

### 实施决策

- 在现有 SettingsContract 注册布尔字段 `free_search_360_enabled`，映射 `FREE_SEARCH_360_ENABLED`，
  智能体的网页搜索分组使用已有 switch 渲染器，补齐中英文文案，不另建保存接口或自定义开关样式。
- 同步配置处理器与控制服务的两份读取投影；缺失、空值与 YAML 异常回退都显示关闭。
  `.env` 已保存值优先于前端服务的旧环境，运行时默认值不覆盖用户已开启的设置。
- DDG、Bing、360 三个开关均归入现有 search reload scope。沿用重载入口，补齐存量 AbilityManager 中免费工具的注册与移除，
  避免只有前端状态改变而已有子智能体工具仍不可用。
- 推荐链路按三种来源中任意一种开启判断可用性，并接受 `360` 来源回执；不再要求 DDG 和 Bing 同时开启。
- 遗留 `mcp_free_search` 的 360 分支从既有 worker thread 调用 SDK 搜索和质量过滤，避免复制 HTML 解析逻辑。
  本轮不迁移遗留 DDG/Bing/fetch，也不把 Work/Code 的 invocation 预算宣称为所有 MCP 的共享预算。
- 依赖上一轮 SDK 360 实现；本轮没有修改 agent-core、依赖锁或用户的运行数据。

### 本轮验证

后端使用上述隔离环境和 protocol `9ec0370` 缓存路径，新增/相关回归共 **182 项通过**，不是对先前 287 项的全部重跑：

- 133 项：`test_free_search_settings.py`、`test_paid_search_configuration.py`、`test_agent_reload_model_scope.py`、
  `test_trusted_search_tool_adapter.py`、`test_topic_recommendation.py`。验证实际配置保存与磁盘回读、刷新来源优先级、
  搜索重载 scope、存量工具注册、仅 360 启用时 SDK 与遗留 MCP 实际调用及质量过滤、推荐回执。
- 49 项：`tests/unit_tests/gateway/test_app_web_handlers.py -k 'config_set or config_save_all or config_get'`，
  验证路由等待 scoped reload 完成后再响应。
- 前端 `npm run test:settings-panel` **65 项通过**，覆盖契约类型、独立字段保存、双语和模块声明。
  本机 Node 20 对 `node_modules/.cache/settings-refactor/` 的 ESM 输出要求 `type=module`，
  仅在忽略的测试缓存内补充该声明后运行，未修改产品 package.json。
- 直接运行 `node node_modules/vite/bin/vite.js build --outDir <repo>/tmp/360-frontend-build` 成功；
  未执行 TypeScript 静态检查、lint 或格式化。构建有既有第三方注释、动态导入及大 chunk 警告。
- Playwright 使用 `tests/fixtures/free-search-settings/` 中真实 SettingsPage、保存队列与模块声明，
  RPC 用隔离 localStorage 替身，不连接真实后端。实际点击验证开启、刷新保留、关闭、保存失败回滚及开关尺寸稳定。
  中英文在 1280x900 与项目最小宽度 584x812 验证无溢出，并人工查看截图；375px 受既有全局 584px 最小宽度约束，未扩展布局范围。
  截图位于 `output/playwright/360-settings-{zh,en}-{1280,584}.png`。临时浏览器与 Vite 服务均已关闭。

浏览器测试与真实后端持久化测试分别验证，不能替代真实模型、线上 360 网络或已安装桌面包端到端验证。
现有环境 protocol 版本仍须按锁文件同步；当前前端产物仅构建到 tmp，需正常重新构建并重启应用才会加载本次代码。

## Jina 召回与同响应对照跟进（2026-11-03）

用户确认补齐 Jina 摘要、减少过滤误杀，并用其真实查询区分源和适配损失。
本轮在 SDK 实现 Markdown 结果块解析、限量 `uncertain` 候选与完整去留诊断；Swarm 不复制过滤器，
沿用 `TrustedWebFreeSearchTool -> SDK invoke -> _on_search_results`。新增回归证明 SDK 的不确定性说明原样透传，
其 URL 仍可进入“搜索发现来源”台账，但不是事实或相关性背书；外部正文仍需验证。

SDK 新增直接依赖 `markdown-it-py>=3.0.0`，环境原已安装 4.0.0；Swarm uv.lock 只同步 openjiuwen 的依赖及 requires-dist 元数据，
保留已有 `D:/agent-core` editable 与所有版本，不安装、升级依赖。前端设置和真实 `.env` 未修改。

实际会话 `web_1a1a00cfd7b_ae00e38f1e54` 的 11 次搜索按 tool_call_id 关联：Bing 4 次低相关和 1 次空结果，
360 天气成功（随后抓取成功），另 5 次空结果。后续独立采集确认 360 会跳到 HTTP 200 验证码页，
SDK 之前误报 empty，现改 blocked；历史失败未存原响应，不能断言全部同因。

12 组查询同响应重放：Jina 摘要 0/119→119/119，保留候选 77→98；DDG HTML 88→96；Bing 22→25。
其中 uncertain 分别为 5、4、2 条，不应把增加的候选当作正确答案。Bing 8/12 项的原始结果列表已跑题，
另 1 项无结果；360 两次遇到验证码后停止，其余查询未测试，不能做完整质量排名。
详细逐查询表、原始响应与过滤损失拆分、边界及复现方法见：
`D:/agent-core/.agents/agent-docs/features/F_20261103_1124_web-retrieval-quality.md` 的“召回损失跟进”。

验证：SDK Web 测试 152 项通过；Swarm 的 `test_trusted_web_search.py` 与 `test_free_search_settings.py` 独立运行，
新增待核验候选来源测试后 **47 passed**。两个仓库不可直接合并为同一次默认 pytest 调用，否则 tests.conftest 同名冲突。
使用现有 venv 与 uv.lock 对应 protocol 源缓存，产物写入 tmp；无真实模型调用、未重启服务、未重打桌面包。
原始搜索响应保存在本地 tmp 不提交；真实任务排障需显式开启 `FREE_SEARCH_DEBUG`，默认不落搜索内容。

## Bing 请求路径诊断跟进（2026-11-03）

用户取消代理对比后，仅进行本机无应用层代理的 HTTP、地区参数和隔离 Chrome 对照。
28 次 HTTP 请求及 12 项有效浏览器直接 URL 矩阵表明，特定请求参数下原始结果和浏览器页面均会跑题；
美国区组合、本次正常首页搜索则能提供同查询的主题候选。不能再把现象泛化为 Bing 没有相关索引，
也不能把小样本改善当稳定性保证。最终网络归因仍缺少独立出口对照。

全部矩阵、首页表单/请求头控制、浏览器初始响应与最终 URL 差异、排除的探针失败及本地证据路径，
统一续写在 `D:/agent-core/.agents/agent-docs/features/F_20261103_1124_web-retrieval-quality.md`
的“Bing 请求路径对照”。本轮未改生产代码、前端设置、真实配置或默认来源/市场，未重启服务；
未重新执行上文 UT，不把既有通过数当成本轮实测结果。本次独立浏览器已关闭。

## Bing 市场路由交付跟进（2026-11-03）

本节为初版计数规则及其验证；备用市场扣次语义以后文“Bing Query 额度修订”为准。

用户确认直接实现，并在开发中明确撤回新增统一“首次 + 3 次”预算的提议，保留现有预算。
SDK 现将查询语言和市场选择解耦，默认 en-US，仅低相关时最多试一次另一市场，并扣除原 Bing 次数及剩余时间。
Swarm 继续原样透传 SDK invoke，不另建重试器；仅在 `.env.template` 增加后台主/备用市场配置并同步中英文说明，
没有新增前端字段、改变既有预算或修改真实用户配置。当前仅 Work/Code 的 SDK 路径使用此策略，遗留 MCP Bing 未迁移。

新增两个来源包装回归通过实际 SDK 路由验证：主市场拒绝的 URL 不进入台账，备用成功 URL 才可记录，
两个市场共用 callback budget，改词后的第三次 Bing 请求被原额度拒绝且不产生网络请求或新来源。
本轮 `test_trusted_web_search.py` 与 `test_free_search_settings.py` 共 **49 passed**；SDK Web 共 **174 passed**。
另使用此前固定响应离线重放，6 个查询从语言绑定基线的 3 个有候选提升到新默认的 6 个；不是线上稳定性或最终答案证明。
完整决策、路由矩阵与限制续写在 core `F_20261103_1124_web-retrieval-quality` 的“Bing 市场路由实施”。

未重启服务、重新打包或执行真实模型任务；旧 SDK/旧进程不会自动获得此修复。本轮无新增运行依赖或锁文件修改，未提交或推送。

## Bing Query 额度修订（2026-11-03）

用户澄清为两个 query 额度：原 query 和改词后的 query 各可执行主市场及一次低相关备用市场，
最多四个市场请求；备用请求不额外扣 Bing 次数，但仍受原剩余时间约束。当前 SDK 只在主市场前预留一次额度，
Swarm 不添加独立重试或计数逻辑，其余来源和既有预算默认值不变。

更新的真实 SDK 包装回归核对两组不同 q 参数及各自的两个市场、预算累计为 1/2，
第三个 query 无网络调用、无新来源；备用成功/失败分别验证台账和模型可见结果。
最终 Swarm 两文件回归 **49 passed**，SDK Web **177 passed**；超时、来源上限和并发场景亦已验证。
模板、中英文配置文档及两仓 spec 已同步，历史初版验证记录保留但不再代表当前计数规则。
本次没有重启服务、重打包、修改真实 `.env`、提交或推送。

## 候选误拒与泛 AI 回退修复（2026-11-03）

用户在新预算逻辑下复测后确认继续修复接入问题。本次不改 Swarm 生产代码或再造过滤/重试器，
SDK 修复已知主题的简繁别名、“点位”的行情意图，以及 AI Agent 复合查询缺少 Agent 主体时的误放行。
真正待核验候选仍按既有契约透传；泛 AI 被拒绝后由原有低相关市场分支处理，默认两 query 四市场上限不变。

新增 3 项实际包装回归验证泛 AI URL 不进入来源台账、备用成功/双失败、callback 预算只扣一次、
繁体行情来源出现在最终工具结果和台账。最终 `test_trusted_web_search.py` 与 `test_free_search_settings.py`
共 **52 passed**，SDK Web **206 passed**。SDK 端的两轮 20 份实际候选离线重放恢复繁体财经/新闻候选、
拒绝泛 AI 页面；未重新请求外网，不能把回放候选改善当作搜索源稳定性或真实模型闭环保证。

完整决策、首次夹具 charset 修正、重放数值、证据路径与限制续写在 core
`F_20261103_1124_web-retrieval-quality` 的“实际案例召回与弱主题修订”。本仓 spec 只记录消费边界。
未修改真实 `.env`、默认引擎、端口或运行进程；无需重建前端，源码服务重启加载配套 SDK 后生效，
已冻结打包的客户端仍需重新打包。未提交、推送或启动代理/浏览器回退。

## 当前模型批量评估接入（2026-11-03）

用户确认用现有 LLM 批量评估替代查询定制规则。本节局部替代前述词法主题、别名与 AI Agent 特判；
来源启用、前端设置、预算和权限/来源记录继续沿用。底层模型协议、选择策略及实测统一留在 core
`F_20261103_1124_web-retrieval-quality` 的“通用批量语义评估替代”，不在 Swarm 再建分类器。

- Work/Code 既有 `TrustedWebFreeSearchTool.invoke` 已透传 callback，SDK 可从当前 Agent.get_llm
  取活动模型。没有修改包装生产逻辑或再创建客户端，原任务由现有 SDK budget Rail 传递。
- 来源台账只记录 SDK 最终选中的 URL，拒绝的 URL 不进入台账；uncertain 与 unavailable 的线索仍须核验。
  新增真实 AbilityManager 分发回归验证活动模型确被调用、原始任务与子问题一致、最终文本与台账排除拒绝 URL。
- 遗留 `mcp_free_search` 的 360 分支没有 Agent 模型，SDK 只做结构检查并标记未评估。此前本仓 renderer
  会丢弃该状态，本次补齐 `Assessment status: unavailable`、`Content status: uncertain` 和逐条原因，
  不悄悄创建 LLM 或假装语义过滤已完成。原生推荐保留 SDK 候选字段，遗留 DDG/Bing/fetch 未迁移。
- `.env.template` 增加后台 `FREE_SEARCH_ASSESSMENT_TIMEOUT_SECONDS=15`，不增加前端模型字段，
  不改真实 `.env`、默认引擎、模型、代理、TLS 或预算值。中英文配置与接入 spec 已同步。
- 定向来源包装与设置回归 **53 passed**。此前 1 项旧测试把无模型的遗留 360 当作已完成语义判断，
  现分别验证显式模型的拒绝、无模型的警告和两条路径的结构检查，而非给生产代码补 query 特例。
  模拟模型回复只测接入，语义效果来自独立固定语料的真实调用；两种证据不混用。

实际复用本机默认 `deepseek/deepseek-v4.1-flash`：12 组/48 候选，运行两轮，含失败的标签一致率
47.9%→90.6%，23/24 批有效；每批中位 1.84 秒、P95 3.62 秒。10/20/30 条的长度补测每批约 3.1–6.2 秒。
仍有 5 条预标 uncertain 被拒绝和 1 批格式错误，不因复测成功删除失败。详表、独立标签和本地报告路径见 core 记录。
没有采集新的搜索引擎响应，也没有测量完整用户任务闭环；不能声称这项改动已使整条 Agent 链路更快。

测试基于 Swarm `8a551009cf09a3a0bcc133d1a84a1ae070857a52` 加已有工作区，以及 editable
`D:/agent-core` 本轮候选；命令/现有 venv/protocol 缓存沿用前节，basetemp 为 `tmp/search-assessment-delivery-swarm`。
自检限本轮增量与真实调用链，不混入其他历史修改。未运行全仓测试、静态检查、前端构建、桌面打包，
未改依赖、启动/重启服务、提交或推送。需要后端重启加载两仓代码，前端无需为此重建；旧冻结包需重新构建。

## 移除 360 来源（2026-11-03）

用户因持续访问限制明确要求整体移除，不只关闭开关。本节替代上述 360 可选来源及前端适配决策，
历史测试记录保留但不再表示当前支持范围。底层解析和 schema 由配套 core 移除；本仓同步删除配置读写、
启动默认值、前端开关/中英文文案、遗留 MCP 分支和推荐启用/回执枚举，不再注册该来源。
不修改真实 `.env`：遗留的 `FREE_SEARCH_360_ENABLED` 被忽略，不能单独启用免费搜索或话题推荐。
显式旧 provider 由 SDK 明确拒绝，不偷偷切换引擎。DDG/Jina、Bing、现有模型批量评估与预算保持不变。
选择删除能力而不是保留隐藏开关，避免存量环境配置再次访问持续被限制的来源。

验证基于 Swarm `8a551009cf09a3a0bcc133d1a84a1ae070857a52` 加已有工作区、本节增量，
以及 editable `D:/agent-core` 配套候选，继续使用前节 venv/protocol 缓存；不将此前全部 HEAD 差异归于本次退役。

- `test_trusted_web_search.py` 与 `test_free_search_settings.py`：**58 passed**，覆盖 DDG/Bing
  独立保存/回读/热更新，旧 360 环境不注册工具、不发请求、不启用推荐、旧 provider 回执被拒绝。
- `TestFreeSearchRuntimeDefaults`、`test_config_set_applies_scoped_reload_before_responding`、
  推荐的 configured/unconfigured gateway 装配入口：**13 passed**。另一次相关门控筛选为 2 passed / 416 deselected，
  不把被排除用例计入通过数。命令使用 `-o addopts= -o log_cli=false -o asyncio_mode=auto -p no:cacheprovider`，
  basetemp 分别为 `tmp/pytest-remove360-swarm`、`tmp/pytest-remove360-startup`、`tmp/pytest-remove360-consumers`。
- SDK Web 回归 **181 passed**；一次测试改写的 URL 解析函数名错误已修复并全量重跑，详见 core 同名退役章节。
- `npm run test:settings-panel`：**65 passed**，该脚本仅 esbuild 与 Node 行为测试，没有运行 tsc/静态检查。
- Playwright CLI + 本机 Chrome 在隔离 RPC 的真实 SettingsPage 中验证：仅 DDG/Bing 两开关、
  旧 localStorage 360 值不显示、独立保存及刷新回读、保存失败回滚、开关尺寸稳定。
  `zh/en` × `1280x900/584x812` 四视图无页面或文本溢出；截图位于本地
  `output/playwright/removed-search-settings-{zh,en}-{1280,584}.png`，检查了中文宽屏和英文窄屏截图。
  隔离脚本在 `tmp/verify-removed-search-settings.js`，不提交运行产物。

自检覆盖本节跨仓移除范围、配置的读写两端和存量值，以及免费/付费注册边界，未发现新增调用残留。
未测试完整远端服务端到端、真实搜索或整条模型任务，未运行全仓测试、静态检查、生产前端构建或桌面打包。
没有修改真实 `.env` 或重启用户服务；临时测试服务（54137）和独立浏览器已关闭。
加载两仓新代码需重启后端，已有前端构建需重建，冻结安装包需重新打包；未提交或推送。

## 消费评估运行时修复（2026-11-04）

SDK 根因、策略与回放证据维护在
`D:/agent-core/.agents/agent-docs/bugfix/harness/B_20261104_1359_search-assessment-runtime.md`，不复制底层实现。
Swarm 仍沿 TrustedWebFreeSearchTool -> SDK 的已有路径消费结果，无新增网络/评估分支和 UI 配置。
S_02 更新 unavailable 候选上限、警告透传与 PDF/WAF 失败约定。

本轮增量基于 Swarm HEAD `4f0f884b7c3b2a0f20565cec69e8bda0b457629e` 加已有工作区，
使用已修复的本地 SDK HEAD `aa0e69bb8c6557109d964dd4f9c2bc3e3320d068` 加工作区。
`test_trusted_web_search.py` **27 passed**；新增真实 AbilityManager 分发测试验证截断评估时第 3/4 个来源
仍进入可见结果和来源台账、第 5 个受 max_results 限制，uncertain/unavailable 警告未丢失、无推荐排名，
且仅消耗一次 Bing 额度、一次评估调用。主模型配置不改变，包装不把线索升级成事实。
测试先激活 .venv，PYTHONPATH 包含两仓，JIUWENSWARM_DATA_DIR 设为工作区 tmp 隔离目录，
命令为 `python -m pytest tests/unit_tests/agentserver/permissions/test_trusted_web_search.py -q -o addopts= -o log_cli=false -p no:cacheprovider --basetemp=tmp/pytest-search-runtime-fixes-swarm`。
最终扩展至 `test_topic_recommendation.py`、`common/test_free_search_settings.py`、`test_web_fetch_metadata.py`、
`agents/test_web_fetch_tools_security.py`，连同来源包装共 **191 passed**；不重复累计前述 27 项。

复用 Swarm 既有模型构建入口完成有限真实模型候选回放，详细数字及限制见 core 故障记录。
未重新跑完整 Agent、改真实配置、重启服务或打包；本轮没有前端生产代码变更，不需要单独重建前端。
使用本地 SDK 的服务需重启；冻结安装包需重新包含修复后的 SDK。

## 官方同步与交付（2026-11-04）

用户授权只提交免费搜索相关实现、测试和文档；本次没有关联 Issue，允许省略 Refs。
同步官方 Swarm `26acdadda` 和 core `8ede5297c`，保留官方 Gateway 的固定修订。
提交版 Swarm 依赖固定到配套 core fork 提交，不能继续使用缺少 WebResearchBudgetRail/结果钩子的旧 SDK；
本机 editable SDK 配置仅保留在工作区，不将机器专属路径作为可分发依赖提交。
交付 core 修订为 `9e4673601bfcdd1292df5337501838b9c2908f96`，GitHub 与 GitCode fork 均已推送；
Swarm 通过 GitCode fork 固定此 SHA，uv overrides 保持 Gateway 传递依赖及各 SDK extras 使用同一版本。
锁文件使用项目 venv 内 uv 0.12.5 生成，避免系统旧 uv 改写无关平台依赖条件；不更新其他依赖版本或现有虚拟环境。
其他浏览器文档、运行数据和不相关变更不进入本次提交，生成索引也按本次文档范围更新。

此前已通过真实 Web Gateway 链路复测 13 个原问题及 4 个 DDG 对照，17 次生命周期均完成、无 chat.error；
这不等于 17 个问题都找到了证据。40 个有效候选批次没有再发生评估不可用，Bing 多个问题的原始页面仍跑题，
DDG 对照部分可用；源响应、页面防护和最终证据核验的限制仍需分别看待。
详细结果与速度统计的解释统一留在 core `B_20261104_1359_search-assessment-runtime`，不复制原始用户轨迹。
完整 Agent 实测发生在官方同步前；同步后另运行定向行为回归，不把之前的线上结果当成合并后重新实测。

合并后的 8 个 Python 测试文件共 **565 passed**，覆盖来源包装、推荐、搜索设置、fetch 消费、
权限冷启动、必需 Rail 装配和 Gateway 配置入口；`npm run test:settings-panel` 为 **66 passed**。
首次追加冷启动文件时未指定 `asyncio_mode=auto`，出现 13 个异步夹具/测试模式错误、252 项通过；
按既有测试约定补上该选项后重新运行完整组合得到上述结果，没有修改产品代码绕过失败。
Python 使用已激活的 Swarm venv、两仓 PYTHONPATH、隔离 JIUWENSWARM_DATA_DIR，pytest 指定
`-o addopts= -o log_cli=false -o asyncio_mode=auto -p no:cacheprovider`；
未运行格式化、静态检查、全仓测试或桌面打包。Core 合并后相关行为回归 **369 passed**。

## 独立官方 PR 交付（2026-11-04）

按用户新授权从官方 `develop` 的 `26acdaddae1fa0daf0f888f8012f0284a2548204` 单独挑选免费搜索改动，
不合入 fork 的 merge 或其他历史提交。关联 `Refs #9193`；该 Issue 还包括 wheel 默认启用搜索的诉求，
本次不修改默认引擎开关，不能把本 PR 描述成完整关闭该 Issue。

本节替代上一节 fork 交付所用的 SDK 修订。配套 core PR 为
https://github.com/openJiuwen-ai/agent-core/pull/2466 ，其分支从官方 `8ede5297c` 出发，
只有免费搜索实现、测试、文档三个连续提交。Swarm 的依赖、所有 extras overrides 和锁文件统一固定为
`3781cec780ce12d88ad4be4a6bf68d0ab6b7a91b`，不再依赖含其他 fork 历史的 `9e4673601`。
消费方 PR 先以 Draft 提交；待 core 合入后改回官方可分发修订并重新生成锁文件，再转为可合并状态。

独立 PR checkout 中重新核对两仓实际导入路径并执行回归：core **369 passed**，Swarm 上述 8 个
Python 文件 **565 passed**，前端 `npm run test:settings-panel` **66 passed**。
使用现有激活 venv、两份干净 PR 源码 PYTHONPATH 和隔离数据目录；原本 develop 工作区及本地 editable
配置保持不变。此候选未重新执行外部搜索/模型 E2E，不把上一节线上结果当作此候选的成功率。
仍未运行全仓测试、格式化、静态检查、生产前端构建或桌面打包。

## GitCode 检视修复（2026-11-10）

本轮针对 !9841 的三条意见，以 `78240cade7bf4b2f069a2a4c5fc0925eab18f009` 为基线。
用户授权检视并修复必要问题，没有授权本轮提交或推送；不修改主工作区、真实配置或现有服务。
随后用户明确授权仅将这三项修复提交并推送到 !9841，关联 GitCode #5651；提交邮箱仅本次使用
`dingyangkai@huawei.com`，不修改仓库默认身份。其他模块的自动索引更新不纳入本次提交。

- 可用性 helper：当前固定 SDK 已导出两个函数，但删除任一导出的回归能复现模块导入失败。
  两个导入分别提供现有环境变量回退，保留可用的 SDK 实现和逐轮热更新；不接入第二套搜索实现，
  也不把其他 SDK 接口不兼容隐藏为正常搜索。覆盖单独/同时缺失、6 个配置源、空白 key、旧 360 标志。
- 非隔离构建：`--no-isolation` 不自动安装后端依赖，但仍检查依赖，不是完全忽略 build-system。
  两个构建脚本显式安装已声明的 setuptools/packaging，使用同一个 Python，安装失败即停止。
  test extra/group 与冻结打包 dev extra 补直接依赖；不增加旧 wheel 后端兼容分支。
- 官方 pin：6 种主依赖、extra、override 中带 fragment/query 的合法官方固定版本在修复前被误拒。
  现在用现有 `urlsplit` 剥离这两个 URL 部分再检查仓库和完整 SHA；fork、浮动分支、短 SHA 仍拒绝。
  锁文件只增加 workswarm 的直接依赖边及声明，332 个包的版本、来源、原有 marker 和 SDK pin 均未变化。

测试使用现有 Swarm venv 的 Python 3.11.5，PYTHONPATH 依次为当前 checkout、
`.tmp/search-pr-10729-upstream-20261110`、`.tmp/search-pr-10570-upstream-20261110` 和
`.tmp/search-pr-10570-compatible-20261106`，以匹配本 PR 固定的官方依赖，不使用 editable Core。
pytest 选项为 `-q -o addopts= -o log_cli=false -o asyncio_mode=auto -p no:cacheprovider --tb=short`。

- 发布门禁、build config、浏览器搜索提示三个文件：**107 passed**，正常退出。
  构建 bootstrap 测试使用 stub Python，分别验证 Bash/PowerShell 安装成功和失败的真实控制流，
  不执行 pip/npm 安装；既有测试另覆盖实际 setuptools wheel/sdist/editable 构建钩子。
  初次沙箱运行的 6 个失败均为 Windows 临时元数据重命名或 Git Bash 启动权限错误，普通用户权限重跑后通过。
- 加入来源包装、必需 Rail 装配、权限冷启动、免费搜索设置四个文件后：**247 项断言通过**，
  JUnit 无 failure/error；但退出时 `tests/conftest.py:35` 清理临时日志被 Windows 文件锁阻止，命令退出码 1。
  不把退出清理问题当产品回归，也不将该命令描述为完整通过；没有为本轮修复修改公共测试清理逻辑。
- `uv lock --check --offline` 和对当前仓库运行 `validate_release_dependencies` 均通过；
  用 TOML 结构对比确认除 workswarm 依赖声明外其他包记录及顶层元数据完全一致。
  JUnit 在本地 `.tmp/search-pr-review-fixes-{unrestricted,expanded}-20261110.xml`，不提交测试产物。

未运行格式化、静态检查、全仓测试、完整桌面打包或真实搜索/模型任务；未安装主环境依赖或重启服务。
当前契约同步到 `S_02_web-retrieval`，不新建管理器、设计文档或重复检索策略。

## CI editable 构建回归修复（2026-11-10）

针对已推送的 `92d33bdf6dc095e38ae41e2ed0c117af453bac93`，用户提供 build、st_gateway、UT 三份
本轮 CI 日志。用户先明确选择只修 build/UT、Gateway 保持不动，随后授权仅将本节修复、测试和记录
提交并推送到 !9841，关联 #5651。本次 author/committer 邮箱使用 `dingyangkai@huawei.com`，
不修改 Git 默认身份，不带入无关自动索引变更；推送后跟进新 SHA 的 CI 结果。

- build（15:51:56）与 UT（15:47）都在 `uv sync --all-extras` 调用
  `setuptools.build_meta.get_requires_for_build_editable` 时失败，尚未执行前端构建或测试用例。
  两者都在 `setup.py:7` 抛 `No module named 'setuptools.command.bdist_wheel'`，后端来自镜像的
  `/usr/local/python3/lib/python3.11/site-packages`。日志未给出完整环境及 setuptools 版本，
  不能进一步认定是哪个 PYTHONPATH 或隔离配置造成旧后端被加载。UT 的上传列表为空，不能当作断言失败。
- 上一节仅补后续 `python -m build --no-isolation` 的安装步骤，未覆盖前置 editable 入口；
  本节替代“不增加旧 wheel 后端兼容分支”的决策。仅捕获缺少指定模块的 `ModuleNotFoundError`，
  回退 wheel 提供的命令；现代后端仍优先，其他导入错误不吞掉，不移除发行包依赖门禁。
  build-system、test extra/group、两个构建脚本显式使用 `wheel>=0.46.3`，避免 wheel 0.46.0/0.46.1
  不再提供旧命令的兼容问题。锁文件新增 wheel 0.48.0 及 workswarm 的对应依赖边，未改已有包版本、来源或 marker。
- st_gateway（15:48:53）是独立的 pip 解析失败：Swarm 固定 Core `2dc7477af89ceedbad550de88aac3291db578506`，
  官方 Gateway `435d3323425e1051c6fe2d1feadcba68b4be020d` 固定 Core
  `f3f8f576164cc405c046e43079c8ad00ee17f0c4`。二者即使都声明 0.1.20，直接 URL 仍冲突。
  uv override 未进入 pip 的 Requires-Dist 解析。核对官方 Gateway develop 仍是后者，且两个 Core
  提交的 Web 检索实现并不相同，不能降级来掩盖冲突。本轮不改 Gateway、SDK pin 或运行时搜索行为，
  不跳过测试、不使用 `--no-deps`；因此不承诺本轮能让 st_gateway 通过。

验证：

- 独立 `.tmp/ci-9841-build-legacy` 环境使用 Python 3.11.5、setuptools 69.5.1、wheel 0.48.0。
  修复前真实 editable 构建重现同一导入异常。修复后直接调用既有构建测试函数完成 **14 项检查**：
  官方/fork 两种依赖各覆盖三类 get_requires 和三类 build hook，另验证 skip-build 不能绕过门禁，
  以及真实项目 pyproject/setup 的 get_requires/prepare_metadata_for_build_editable。
  这不是全套 pytest：独立环境没有应用依赖，直接复用同一测试函数及断言隔离构建入口。
- 现有环境 setuptools 82.0.1 上，发布门禁、build config、浏览器搜索提示三个文件共 **117 passed**，
  包括现代/旧模块选择、无关 ImportError 透传、依赖探测和实际项目元数据回归。
  使用上一节官方 SDK 源码覆盖与 pytest 参数；报告为本地 `.tmp/ci-9841-build-fix-modern.xml`。
- `uv lock --check --offline` 通过。TOML 结构对比确认 332 个已有包中只有 workswarm 依赖声明变化，
  顶层元数据及平台 marker 不变，新增 wheel 后总计 333 个包。

未运行全仓 UT、完整前端/桌面打包或 CI 镜像重建；远端 CI 需推送后重跑验证。
没有改变主工作区、主虚拟环境、Git 默认身份或用户服务，也未新增跳过用例。

## 合并官方 develop（2026-11-11）

!9841 以 `e5bbb24d5` 为源分支基线，合并官方 `b98ec8c7d`。
解决 pyproject 的 source 冲突并重新生成文档总索引，保留 Core 官方 `2dc7477a`，
Gateway 引用跟随官方 Swarm 更新为 `54e5330d3ea6be972aa38de4c42a898ea73fed37`。
官方删除的已迁出 Gateway 测试保持删除，没有借此跳过本地新发现的群任务测试。

用户明确要求暂不提交 11 月 10 日的群锁取消修复；对应运行时代码、测试清理、新用例和故障记录
单独备份，不进入本次合并。主工作区环境不变，使用独立锁定环境验证本 PR 候选。

- Python 3.11.5，真实安装官方 Core `2dc7477a`、Gateway `54e5330d`，没有本地 SDK 覆盖。
- 发布依赖门禁、真实构建 hook、搜索/浏览器路由、提示词、配置与推荐逻辑：**441 passed**。
  Windows 日志退出清理仅由临时测试启动器完成，不进入候选提交；未绕过任何测试断言。
- `npm run test:settings-panel`：**67 passed、1 failed**。失败为
  `modelSettings.test.mjs:721` 的源码反向断言：测试禁止 `MODEL_SELECTION_REFERENCED`，
  但 `ModelsSettings.tsx:457` 已使用此错误码。两个文件均与官方目标分支一致，本次不改动。
- `uv lock --check --offline` 通过；未运行全仓 CI 或声称 st_gateway 安装通过。

核对时官方 Gateway develop 为 `127851ab`，SDK pin 仍是 `f3f8f576`，依赖对齐 PR !76 未合入。
因此 Gateway 代码虽已前移，pip 的两个 Core 直链冲突仍存在；不能将 Core 降级来隐藏问题。

## 再次同步官方 develop（2026-11-12）

用户授权合并官方 `737041a075c79c1bb0b10aa8e27252ca0df670ac` 并推送 !9841；
源分支基线为 `8752a4011de9d0472ea6b7dac8dee5853421c48a`。保留本轮之前已提交的搜索、构建和
群锁取消修复，合并官方其他更新；原有三个未提交文档索引单独备份，不作为本轮新增实现提交。

官方 !9918 已移除母仓对 Core 的直接依赖及 uv override，解决的是 wheel/pip 中同名 SDK 的
双 Git URL 约束。但该变更仍经 Gateway `54e5330d` 选择 Core `f3f8f576`，不是 SDK 升级。
核对官方 Gateway develop 的 pyproject 仍使用该 Core，Gateway !76 仍未合入；
`f3f8f576` 中不存在本 PR 所需的 `openjiuwen/harness/rails/web_research_rail.py`。
用户明确选择本次更新先保留 Core `2dc7477a` 及配套 override/source，待 Gateway 官方 pin 前移后
再删除直接 SDK pin，采用 !9918 的单一传递来源。不能以降级搜索能力换取安装阶段通过。
本轮不修改 Gateway 业务代码，不采用个人 fork 依赖，也不声称 pip 的 st_gateway 冲突已经修复。

群锁冲突继续使用 `_finish_group_lock_io` 等待获取/释放完成后传递取消，保留上一轮回归覆盖，
而不是恢复为未完成的后台释放回调。文档索引按合并后的源文档重新生成，不手工选择丢弃一边。

合并候选验证使用独立 `.tmp/pr9841-merge-venv`，Core 为官方 `2dc7477a`，无本地 SDK 覆盖：

- 12 个后端文件：**379 passed、6 failed、1 skipped**，未新增跳过。
  搜索、构建、配置、推荐、群锁和续跑用例均通过；失败全部为真实 Worker 启动后的 IPC 等待超时。
  本地子进程 stderr 明确记录 `Worker source changed after the parent validated it` /
  `Invalid Worker parent identity`。这定位到父子身份校验，但不能据此替代缺少子进程日志的 Linux CI 根因证据。
- 单独运行 recovery 失败用例，附只读父子指纹诊断：**1 passed**；代码和环境指纹均一致。
  收集相同 12 个文件但只运行该用例也通过，进一步将本地问题缩小到先前用例的执行副作用；
  不提高 timeout、不关闭身份校验。
- `npm run test:settings-panel`：**67 passed、1 failed**，仍为上轮记录的模型删除错误码源码断言。
  直接运行旧 fixture 缓存曾出现设置键不一致，重新通过 npm 脚本构建 fixture 后该项通过，
  不将缓存不一致误报为产品回归。

进一步确认 `test_setup_selects_the_available_wheel_backend` 在 pytest 进程导入 `setuptools`，
后者将自身 `_vendor` 加入 `sys.path`。独立复现中，该导入不修改安装包或源码，却使
`environment_identity()` 从 `7656803f` 变为 `6013c38f`，多枚举 12 个 vendor distribution。
Worker 新进程没有该导入副作用，启动校验因此拒绝运行；这解释了单独通过、组合失败的本地现象。

本轮追加最小测试隔离：只在 `test_release_dependencies.py` 的 autouse fixture 为每个用例提供
独立 `sys.path` 列表，pytest monkeypatch 退出时恢复原对象。不修改生产 dependency identity、
SDK/Worker 行为、超时或断言。第一次修复后同组 **383 passed、2 failed、1 skipped**，Worker
用例全部通过；两项 editable 构建失败为新临时目录过长触发 WinError 206。
最终使用 `.tmp/t9841a` 重跑同一完整组合：**385 passed、1 skipped**，包括原报告五项 Worker
失败用例及本地额外复现的第六项。JUnit 为本地 `.tmp/ut9841-update-final-20261112.xml`，不提交。
`uv lock --check --offline` 通过，锁文件内容仍与合并前一致，没有修改已有包版本或 marker。

未运行全仓 UT、Linux CI、完整桌面构建或真实搜索 E2E；主工作区、主环境和用户服务不变。
远端五项 UT 是否为相同根因仍需新 CI 回执，不能把本机复现等同于取得了 CI 子进程 stderr。
Gateway pin 前移后仍须重锁并重新验证普通 pip 安装。

## 采用 Gateway 单一 SDK 来源（2026-11-12）

用户随后明确改为由 Gateway 传递 SDK，接受等待 Gateway 后续更新。本节替代上一节的
"暂时保留 SDK 直接 pin" 决策，不改写之前使用 Core `2dc7477a` 的验证事实。

- 移除 Swarm 主依赖中的 Core 直链、五个 SDK uv override 及 SDK source；Harmony 改为
  `openjiuwen>=0.1.20`，其余 SDK extras 保留。Gateway 固定来源仍为官方 `54e5330d`。
- 不修改 Gateway 仓库、搜索实现、缺失能力的显式错误或已有搜索测试；不以跳过用例掩盖 SDK 差距。
- `MANIFEST.in` 将 uv.lock 放入 sdist，使仅有传递 SDK 时，从 sdist 重建 wheel 仍可通过原有
  官方依赖门禁；不放宽门禁，也不将锁文件装入 wheel。增加真实 sdist -> wheel 回归，检查 wheel
  元数据仅通过 Gateway 获取基础 SDK，保留可选 extras。
- 后续 Gateway 更新 SDK 后，还需要更新 Swarm 的 Gateway SHA 并重新锁定，不能把当前
  传递依赖调整表述为已经使用最新 Core。

验证：

- `uv lock --check --offline` 通过；333 个包中只有 workswarm 与 openjiuwen 的声明改变，
  其他包版本、包内容与平台 marker 均保持原样。SDK 当前为 Gateway 指定的 `f3f8f576`。
- `uv sync --locked --all-extras --offline --dry-run` 通过。另建独立
  `.tmp/pr9841-transitive-venv`，实际执行 `uv sync --locked --no-default-groups --group test --offline`，
  成功安装 280 个包；`uv pip check` 通过。没有使用 SDK override 或本地源码替换。
  下载该 SDK 的 Git 缓存获取失败后，仅从本机既有 Git 仓补入相同官方完整 SHA 对象再构建。
- 发布依赖与 build config 的两个测试文件在已有环境和上述真实锁定环境均为 **74 passed**，
  覆盖原生 editable 元数据、官方来源检查、保留 extras、旧 setuptools 入口与 sdist -> wheel。
- 在新锁定环境追加 `test_trusted_web_search.py` 后为 **74 passed、31 errors**。
  31 项均在 fixture 初始化读取 `free_search.resolve_search_model` 时失败，旧 SDK 未提供该接口；
  测试正文尚未执行，不把它们表述成 31 个独立业务回归，也不跳过测试。
  JUnit 在本地 `.tmp/ut9841-transitive-sdk-20261112.xml`，不提交运行产物。

这次消除了 Swarm 与 Gateway 对 Core 的两个不同直链声明；未运行远端 st_gateway 或声称
整套 CI 已通过。Gateway 官方固定提交前移后，需重锁并重跑搜索及 ST，才能交付本 PR 的完整搜索能力。
主工作区环境和已有三个未提交索引不纳入本次提交。

## 残余限制

语义评估仍可能误判或不可用，不能把所有相关搜索结果变成实时证据。剩余搜索源和页面布局也可能变动。
默认预算不是全团队配额，也不能阻止 LLM 调用浏览器/其他工具或继续思考；不能声称完整闭环耗时已得到实测改善。
自定义 MCP 与遗留 MCP 的 DDG/Bing/fetch 不在 SDK 语义评估替换范围。
当前 Work/Code 消费方必须与配套 SDK 一同交付。
