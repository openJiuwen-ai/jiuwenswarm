---
id: F_20261027_1650_enterprise-1030-integration
title: 1030 企业版同步 develop 的集成候选
type: feature
author: ZYQ5333
author_email: zhangyaqing8@huawei.com
designs: []
design_note: 用户在当前会话明确授权从 release/1030 创建 integration 并整合主线。
modules:
  - gateway
  - jiuwenswarm/server
  - jiuwenswarm/runtime
  - jiuwenswarm/agents
  - jiuwenswarm/extensions
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/runtime/service.py
  - jiuwenswarm/server/runtime/tenant_agent_pool.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - gateway/message_handler/message_handler.py
  - gateway/channel_manager/web/web_connect.py
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/InputArea.tsx
  - jiuwenswarm/common/schema/message.py
  - gateway/extensions/sdk/application_plugin.py
  - tests/unit_tests/common/test_enterprise_protocol_identity.py
---

# 1030 集成候选记录

## 同名文件旧主线回退检查（2026-10-28）

对 b901e8cb6 到候选的全部变更中仍两侧存在的文件，逐路径比较候选 blob 与主线历史 blob，
找到 10 个完整等于旧主线内容的候选。此检测不覆盖局部混合回退，不能当作全量语义审查。
恢复 5 个无企业差异依据的文件到 b901e8cb6（这些文件与检查时 upstream/develop 396819cad 内容亦一致）：

- scripts/installer.iss：恢复 XinchenZzz 在 49aefe967 引入的 Windows CLI/SDK/配置升级保全。
  d31b42ac1 的主线父提交已有 PreserveWindowsCli，但合并结果未保留；e99b50320、d52c933d9 延续旧结果。
- team/bootstrap.py：恢复 a54fd726a 的 install_expert_catalog 注册，避免专家目录实现存在但启动未装配。
- PageCard.css / PageCard.tsx：恢复 52ef4929c 的主操作按钮文档流占位，避免长标题悬停时被按钮遮盖。
- test_run_channel_isolation.py：恢复 7d9b024e9 的权限能力绑定、异常恢复和 SDK 回调测试；当前实现已支持这些契约。

另外 5 个候选保留：common/client/agent_client.py、third_agent.py、common/schema/event_base.py、
common/security/base_crypto.py 为 gateway_protocol 共享类型身份的企业契约；extensions/agent_client/extension.py
仍用于 Yuanrong 客户端注册，后续随新增主线 6fb32fce0 一起迁移，不直接覆盖。
检查 scripts/docs/.github/.gitcode 相对已合入基线的剩余净差异，安装器恢复后无其他变更。
最新主线新增的 desktop-electron-packaging.md / check_layering.py 增量不算本轮历史遗漏。

验证：后端定向 60 passed / 37 skipped（Windows、PowerShell、平台工具条件），日志
/private/tmp/integration-stale-file-tests.log；前端 agent-management 82 passed，日志
/private/tmp/integration-stale-ui.log。未在 Windows 编译或执行安装/升级。
本轮只续写现有记录，未重新生成索引或批量修改署名。

## 历史删除残留修复（2026-10-28）

本轮仅清理已合入基线 b901e8cb6 的历史残留，不扩大为新增主线提交的合并。
旧 A4P 文档最初来自 yukuanj 的 b9bd54256，经回退后由 4d2135fc2 重新引入；
企业侧 4bcb7661e 是单父批量同步，未保留对应主线合并祖先。
主线 571bec10f 删除旧文档并迁移六个 A4P 模块；后续三方合并的共同祖先无旧文件，
企业侧旧副本被当作新增保留。本文档现删除，旧 A4P 六模块与 Hook 两别名一并删除；
Runtime organization/ops 处理器转用 common.a4p 包，聊天中间件改用 gateway_protocol.hooks。
新增 Runtime A4P 分发回归并验证 WebSocket/中间件 Hook 行为；旧推送测试桩定位到 common.host_services 唯一总线。

本轮回归：168 个文件，4014 passed / 3 skipped，64.67 秒。日志 /private/tmp/integration-deletion-final.log；清单 /private/tmp/integration-deletion-recovery-tests.txt。环境沿用前轮，未做部署/E2E 验收。
推送前远端已推进到 2a9252424（250 文件企业迁移/修复）；以普通 merge 保留该批提交。最终合并版本定向复验 A4P、聊天 Hook、插件加载共 134 passed，3.03 秒，日志 /private/tmp/integration-deletion-remote-final.log。4014 项结果对应合并远端增量前的修复版本，不能当作最终合并版本全量通过。


按全仓历史扫描（关闭重命名检测，将迁移旧路径也计入）相对已合入 b901e8cb6 的删除残留清零。
此前相对最新 833536d7b 找出的另外 20 个扩展文件不是历史漏删：删除/迁移它们的 6fb32fce0
在 2026-10-28 新增且尚未进入 integration。当前 Gateway/Runtime 插件装配、ADX/AgentOS 和
Yuanrong 客户端仍消费这些文件，故本轮保持，下一轮合并新主线时按其迁移同时更新消费方。
这纠正了上一轮将最新主线迁移候选与历史残留并列而未充分区分基线的判断。

后续同步约定建议：长期集成分支使用普通 merge 保存祖先，不 squash develop 的同步提交；
每轮固定同步前 SHA 和上游 SHA，分别检查主线删除/重命名清单与 PR 净差异。
对“上游不存在但企业存在”的文件逐项确认是企业新增、明确保留还是迁移残留；
引用检查包含 Python 导入、动态加载和 extension.yaml。祖先包含检查与 UT 通过均不能替代内容复核。
release/1030 修复回流 integration；1110 特性继续在开发线，避免反向混入客户 release。

## 当前 PR 冲突处理（2026-10-27，基线 b901e8cb6）

本节替代上一轮的当前基线和“不 push”交付状态，历史验证证据保留。
本轮从已推送的 integration `c0ffec2e25deb6c98101c8f4e1199920ce507cc5` 合并
upstream/develop `b901e8cb611110a56ad27b76a64cfab5ecdd2cc5`，包含主线新增的 11 个提交。
提交前再次 fetch 两端，版本未变化；使用普通 merge，不改写历史，交付目标为推送 origin 的 integration 以更新 PR。
不直接修改 upstream/develop。原工作目录的 feature/enterprise-verify 与本地 release/1030 保持不变；
本轮读取到 origin/release/1030 已由其他工作推进至 `e0945addc76db571a7542b789f5afea2fdf68fa0`，本任务未改动该分支。

三个文本冲突的处理：

- `gateway/cron/controller.py` 同时保留企业身份字段保护与主线 scheduled-task policy。
- `gateway/cron/scheduler.py` 先拒绝不支持的小艺定时任务，再沿用企业 DB 调度、租户三元组与项目准入路径；未覆盖为个人版调度实现。
- `ChatModelSelector.tsx` 使用主线显式 sessionId 绑定设计画布的选择，同时保留企业版 disabled 限制，避免改到其他会话或绕过模型权限。

自动合并部分保留主线 server_store_bridge 退役、MCP 远程回调、轨迹 AgentServer 代理及共享协议成员注册；
核对 Gateway 装配、Web 客户端绑定和消费者，并运行相应回归。未恢复已退役本地桥。

本轮行为证据：

- 后端原 138 文件加本轮新增/修改测试，共 148 文件：**3550 passed / 3 skipped，65.28 秒**。
  日志 `/private/tmp/integration-conflict-backend.log`；实际清单 `/private/tmp/integration-conflict-tests.txt`。
- 随后将小艺立即执行/重载调度用例参数化为 personal/enterprise 两版；调度文件复验 **148 passed，4.03 秒**，
  日志 `/private/tmp/integration-conflict-cron-final.log`。此数字与上述大轮有重复，不相加。
- 前端 8 组 **58 passed**：chat-model-selection 13、xiaoyi-cron-target 2、cron-target-keys 2、
  agent-avatar 19、designer-store-save 2、designer-materials 6、designer-material-filename 5、designer-chat-history 9。
  日志 `/private/tmp/integration-conflict-frontend.log` 与 `/private/tmp/integration-conflict-frontend-retest.log`。
- 四个设计画布测试最初在导入 webClient 时缺 window；补用共用 browserEnvironment fixture，并将相关 esbuild 的
  import.meta.env 注入补齐后通过。未改变业务代码来迁就 Node，未放宽行为断言。

环境沿用下文已记录 Python/Runtime/依赖源码与 Node 环境。没有运行格式化、静态检查或生产构建。
本轮为合并增量及相关回归验证，不代表全仓 UT、真实客户联调或部署验收；下文服务级冒烟限制继续有效。

## 上一轮结论（2026-10-27，基线 ef7473ba7）

本记录续写同一集成任务。以下当前结论替代历史章节的“未处理/阻断”状态；旧测试数字保留为当时证据，不代表当前候选。

- 分支：`feature/enterprise-integration`；源 `release/1030` / `origin/release/1030` 均保持 `13b737e3e7c8ac9d28663ff7864871c21c04b9d9`。
- 原工作目录 `/Users/openjiuwen/PycharmProjects/jiuwenswarm` 保持 `feature/enterprise-verify`，已跟踪文件未改动。集成位于独立工作树 `/Users/openjiuwen/.codex/worktrees/enterprise-integration/jiuwenswarm`。
- `d52c933d9` 为第一轮集成；`f1adfcbe4` 恢复历史丢失主线行为并合入 `1807d2c8ad710f7fcaeef8fe3c3ac16809b46534`。
- 本次最终合并固定于最后取回的 upstream/develop `ef7473ba73de1e2bcc32c3c9f4f12a599373c58e`（轨迹导入修复）。这是明确的验证基线，后续远端继续前移时需另行核对增量，不能把此处结果直接用于新版本。
- 本地合并与修复完成后再评估上游 PR；本次不 push、不创建或合并上游 PR。

## 为什么“已经 merge”仍会遗漏主线

历史合并 `e99b50320abdbf7d75c33a75b24da4d5fc9354e9` 的双亲为 `9cc94fe1a04b707c705dfb711e8603dec65ec3b9` 和 `77a9c4784bf99faa815df6317feaa88e274da31c`。
当时主线提交已经进入祖先链，但部分实现被合并结果覆盖掉，相关测试仍保留。后续 Git merge 只处理共同祖先之后的增量，不会自动重放这些已被记录为合入的旧代码。因此祖先检查通过不能证明功能完整，release 与初始 integration 相同也不能证明它们与 develop 等价。

本次对照主线实现、消费方和测试恢复了模型 recipe/cache、MoA/CUA、AgentOS 探测与安装命令、流与团队状态、生命周期、Web RPC 等遗漏；没有用覆盖整个企业分支的方式消除差异。

## 实际修复与企业边界

| 范围 | 主线行为恢复 | 保留的企业行为 / 集成修复 |
|---|---|---|
| 模型与 Harness | lazy recipe/model cache、模型选择、MoA/CUA、kernel lease、attach 与流终态 | 租户配置/策略加载、技能、动态 MCP token；kernel 资源身份使用可信租户根路径 |
| AgentOS 与外部 CLI | router probe、endpoint 与命令、install/status/remove、依赖修复安装 | 延续企业接入方式；相关 AgentOS 文件已与本次主线一致 |
| Gateway 与 Web | 专家请求聚合、错误/完成路由、prompt suggestion、A4P 重连、group/history/assets RPC、会话处理状态 | HTTP 出站和 delivery registry、租户消息范围、租户生命周期轮询；保留 AgentServer 的权威处理状态 |
| cron 与文件 | 空内容终态仍记录 last_session_id、流式事件转发、远程图片/文档链路 | DB/Redis 定时调度与审批停放、三元组转发、OBS；下载签名绑定租户且拒绝缺失身份 |
| Runtime | 权限 ACK 经 callback 提前回执、交互恢复和流提前关闭时清理 | `AgentRuntime` 独占一个 TenantAgentPool，池内继续按租户缓存；不再让一个 Runtime 清理其他 Runtime 的 Manager |
| 前端 | 专家团身份/组织、头像、历史回放、主线最新模型/轨迹/输入草稿更新 | 企业模式限制与登录/HTTP 能力保留；未创建专家成员状态时使用稳定空数组，消除反复渲染 |

最新 develop 两处文本冲突已解决：Runtime 控制答案保留企业请求上下文且接入主线 ACK callback；对应 source 参数测试保留三种交互来源。

## UT 失败原因与处置

| 类别 | 具体证据 | 处置 |
|---|---|---|
| 历史实现遗漏 | 模型缓存/recipe、AgentOS `_third_agent_probe_port`、CLI 安装、专家流状态和 Web 方法缺失 | 恢复生产实现，保留行为断言；不是通过放宽测试掩盖问题 |
| 企业架构变化 | Gateway 路径迁移、共享协议枚举、租户哈希/签名、HTTP 附件接口、MCP provider 注入 | 改测试导入、mock 边界和当前返回契约；验证租户拒绝、隔离和远程转发 |
| 真实 Runtime 回归 | 进程全局池忽略后续注入 Manager，Runtime.close 清理其他实例；外层流没有传播 aclose | 改为 Runtime 所有的租户池；用 aclosing 关闭内层流；验证双 Runtime 隔离和交互控制释放 |
| 上游退役 API 留下旧 UT | 主线 `987a40ea7` 已删除 cancel_all_team_stream_tasks，最新 develop 仍残留 5 个调用测试 | 替换为当前 Team Session lifecycle 关闭、失败清理、幂等验证；没有恢复废弃全局 API |
| 提示与前端测试漂移 | A4P 当前提示更严格；CSS 共享选择器、下载翻译仍被消费；虚拟列表无布局时只渲染可见子集 | 按实际有效契约改断言，保留授权前置、范围限制、拒绝、完整导出等要求 |
| 测试环境/隔离 | Node 导入 HTTP/WS 选择器前没有 window/import.meta.env；SQLAlchemy 独立模型重复注册；异步 finalizer 未等完 | 初始化 JSDOM、隔离 DeclarativeBase、等待持久状态与清理；登录探测 fixture 返回确定 404 防止启动退避计时器 |
| 依赖版本 | 本机旧 Runtime v0.1.0 缺 link_mtls_config | 使用官方 Runtime develop `6f582b9ecd57cd08415cf2e37c9a7c953d940c61` 源码验证；客户镜像版本仍须对齐 |

## 最终验证证据

后端：138 个文件，**3384 passed / 3 skipped，59.68 秒**。日志 `/private/tmp/integration-latest-unified-final.log`；清单见附录。
三项跳过为两个 Windows 专属场景与主线已有的本地 session.list fallback 退役占位，不是本次新增 skip。
此前 `1807d2c8a` 候选的 2614/3 结果已由本轮更广验证覆盖。

前端：22 组脚本/入口，**673 项通过**。统一日志 `/private/tmp/integration-frontend-unified-final.log` 首轮 672/673；唯一剩余组织测试因抽取组件片段漏带新常量，已适配提取真实常量作用域，复验 `/private/tmp/integration-team-final.log` 58/58。输入框完整 93/93 且进程正常退出，另见 `/private/tmp/integration-input-final.log`。

| 前端入口（test: 前缀省略） | 通过数 |
|---|---:|
| agent-management / agent-group-management / team-organization / agent-avatar | 82 / 14 / 58 / 19 |
| input-area-permission-merge / session-input / composer-draft-hydrate / new-conversation-draft-preserve | 93 / 63 / 2 / 1 |
| settings-panel / heartbeat-schedule-convert / trajectory-window / trajectory-diagnosis | 64 / 13 / 59 / 92 |
| subagent-history / asset-name-search / auth-session / organization-leader-identity | 45 / 1 / 11 / 3 |
| session-assets / history-recovery / a4p-authorization / market-search-client | 13 / 4 / 18 / 3 |
| chat-model-selection / cron-target-keys（esbuild 入口） | 13 / 2 |

测试环境：Python 3.11 既有 venv；agent-core `b2a0a248c554753ba31727415fe17a03eed60b46`、openjiuwen-protocol `10a03dd263c33042558bbd67a815952458a25fa9`、gateway-protocol `6edf9e36f8de8448b14339712217a263808626bb`、A4P `1699ea3`、上述 Runtime 源码及临时补齐依赖。每轮单独数据目录，`--asyncio-mode=auto` 确保异步用例实际执行。Node v24.14.0，前端用 esbuild + Node test runner。

复验命令形态（需准备上述依赖环境）：

```bash
PYTHONPATH="<仓库>:<Runtime foundation>:<Runtime management>:<锁定依赖源码>" \
  python -m pytest -o addopts= --asyncio-mode=auto --timeout=45 <附录中的测试文件>
cd jiuwenswarm/channels/web/frontend
npm run test:input-area-permission-merge
npm run test:team-organization
# 其余脚本按上表执行。cron 入口避免 package 脚本中的 tsc：
./node_modules/.bin/esbuild src/components/CronPanel/constants.ts --bundle --platform=node --format=esm --outfile=node_modules/.cache/cron-target-keys/components/CronPanel/constants.js
node --test tests/cronTargetKeys.test.mjs
```

## 证据边界与上游合入前条件

完成了上述问题与最新冲突的代码复核、受影响调用链和行为回归；不把此范围表述为整个企业增量的全面检视或全仓 UT。
按仓库约定未执行格式化/静态检查。未完成干净锁定安装、生产构建、真实个人/企业服务启动、Docker/K8s、多进程租户并发、真实模型/数据库/OBS 与客户联调。
这些 UT 证明覆盖路径的行为，不能保证所有主线功能零回归。上游 PR 前应固定客户 Runtime/镜像版本，并完成个人版基本聊天、企业租户并发与越权拒绝、文件上传下载/OBS、审批恢复/取消、定时任务/归档、升级回退的服务级冒烟。

## 后端回归文件清单

<details>
<summary>138 个实际执行文件</summary>

```text
tests/agents/swarm/test_swarm_assembly.py
tests/unit_tests/agentserver/mcp/test_baidu_netdisk_package.py
tests/unit_tests/agentserver/mcp/test_call_timeout_patch.py
tests/unit_tests/agentserver/mcp/test_command_mcp_handler.py
tests/unit_tests/agentserver/mcp/test_delete_custom_mcp.py
tests/unit_tests/agentserver/mcp/test_disconnect_skill_cleanup.py
tests/unit_tests/agentserver/mcp/test_fetch_mcp_tools_resolve.py
tests/unit_tests/agentserver/mcp/test_gitcode_env_token_auth.py
tests/unit_tests/agentserver/mcp/test_host_oauth_flow.py
tests/unit_tests/agentserver/mcp/test_host_oauth_package_runtime.py
tests/unit_tests/agentserver/mcp/test_mcp_cancel_connect.py
tests/unit_tests/agentserver/mcp/test_mcp_cli_auth_poll.py
tests/unit_tests/agentserver/mcp/test_mcp_cli_driver.py
tests/unit_tests/agentserver/mcp/test_mcp_cli_install_only.py
tests/unit_tests/agentserver/mcp/test_mcp_config_placeholder.py
tests/unit_tests/agentserver/mcp/test_mcp_credential.py
tests/unit_tests/agentserver/mcp/test_mcp_dual_source_routing.py
tests/unit_tests/agentserver/mcp/test_mcp_exception_group_handling.py
tests/unit_tests/agentserver/mcp/test_mcp_finalize.py
tests/unit_tests/agentserver/mcp/test_mcp_finalize_cli_auth.py
tests/unit_tests/agentserver/mcp/test_mcp_hub_marketplace.py
tests/unit_tests/agentserver/mcp/test_mcp_hub_node_launcher.py
tests/unit_tests/agentserver/mcp/test_mcp_init_failure_isolation.py
tests/unit_tests/agentserver/mcp/test_mcp_manifest_runtime.py
tests/unit_tests/agentserver/mcp/test_mcp_package_manifest.py
tests/unit_tests/agentserver/mcp/test_mcp_preflight_sse.py
tests/unit_tests/agentserver/mcp/test_mcp_servers_merge.py
tests/unit_tests/agentserver/mcp/test_mcp_skill_scan_dirs.py
tests/unit_tests/agentserver/mcp/test_mcp_skills_listing.py
tests/unit_tests/agentserver/mcp/test_mcp_state_store.py
tests/unit_tests/agentserver/mcp/test_mcp_targeted_control.py
tests/unit_tests/agentserver/mcp/test_mcp_targeted_control_session_fanout.py
tests/unit_tests/agentserver/mcp/test_mcp_token_flow.py
tests/unit_tests/agentserver/mcp/test_register_custom_mcp.py
tests/unit_tests/agentserver/mcp/test_session_mcp_scope.py
tests/unit_tests/agentserver/mcp/test_skill_manager_mcp_skills.py
tests/unit_tests/agentserver/mcp/test_skill_only_mcp.py
tests/unit_tests/agentserver/test_adapter_kernel_invalidation.py
tests/unit_tests/agentserver/test_cron_skip_permission_interrupt_rail.py
tests/unit_tests/agentserver/test_cua_subagent_wiring.py
tests/unit_tests/agentserver/test_deep_adapter_interrupt.py
tests/unit_tests/agentserver/test_deep_adapter_lazy_root_instance.py
tests/unit_tests/agentserver/test_deep_adapter_login_model.py
tests/unit_tests/agentserver/test_deep_adapter_mcp_probe.py
tests/unit_tests/agentserver/test_deep_adapter_model_resolve.py
tests/unit_tests/agentserver/test_deep_adapter_process_cli_run.py
tests/unit_tests/agentserver/test_deep_adapter_session_teardown.py
tests/unit_tests/agentserver/test_deep_adapter_stream_end_final.py
tests/unit_tests/agentserver/test_deep_adapter_sys_operation_release.py
tests/unit_tests/agentserver/test_enterprise_adapter_integration.py
tests/unit_tests/agentserver/test_enterprise_file_http.py
tests/unit_tests/agentserver/test_enterprise_skill_http_route.py
tests/unit_tests/agentserver/test_external_harness_helpers.py
tests/unit_tests/agentserver/test_moa_runtime.py
tests/unit_tests/agentserver/test_moa_session_lifecycle.py
tests/unit_tests/agentserver/test_model_recipe_preparation.py
tests/unit_tests/agentserver/test_obs_file_delivery.py
tests/unit_tests/agentserver/test_system_prompt_restructure.py
tests/unit_tests/agentserver/test_verified_download_assets.py
tests/unit_tests/channel/test_trajectory_http.py
tests/unit_tests/channel/test_trajectory_usage_http.py
tests/unit_tests/channel/test_web_channel_dual_protocol.py
tests/unit_tests/channel/test_web_channel_mode_type_validation.py
tests/unit_tests/channel/test_web_channel_organization_processing.py
tests/unit_tests/channel/test_web_channel_personal_context_payload.py
tests/unit_tests/channel/test_web_channel_stream_coalescing.py
tests/unit_tests/channel/test_web_channel_symphony_status.py
tests/unit_tests/channel/test_web_channel_user_id.py
tests/unit_tests/channel/test_web_channel_ws_sessions.py
tests/unit_tests/channel/test_web_proxy_channel.py
tests/unit_tests/common/test_enterprise_protocol_identity.py
tests/unit_tests/common/test_mcp_dynamic_token_patch.py
tests/unit_tests/common/test_req_method_contract.py
tests/unit_tests/design/test_audio_locks.py
tests/unit_tests/design/test_clip_scope.py
tests/unit_tests/design/test_content_language.py
tests/unit_tests/diagnosis/test_diagnosis_host.py
tests/unit_tests/diagnosis/test_diagnosis_http.py
tests/unit_tests/diagnosis/test_diagnosis_snapshot.py
tests/unit_tests/e2a/test_gateway_normalize.py
tests/unit_tests/extensions/test_adx_control_files.py
tests/unit_tests/extensions/test_adx_environment.py
tests/unit_tests/extensions/test_adx_router.py
tests/unit_tests/extensions/test_adx_router_network.py
tests/unit_tests/extensions/test_agentos_agent_manager.py
tests/unit_tests/extensions/test_agentos_router.py
tests/unit_tests/gateway/test_adx_gateway_identity.py
tests/unit_tests/gateway/test_app_web_handlers.py
tests/unit_tests/gateway/test_config_poll_revision.py
tests/unit_tests/gateway/test_config_repository_order3.py
tests/unit_tests/gateway/test_cron_controller_proactive_tick_guard.py
tests/unit_tests/gateway/test_cron_controller_project_binding.py
tests/unit_tests/gateway/test_cron_db_store.py
tests/unit_tests/gateway/test_cron_factory.py
tests/unit_tests/gateway/test_cron_job_mode.py
tests/unit_tests/gateway/test_cron_login_model_binding.py
tests/unit_tests/gateway/test_cron_model_validation.py
tests/unit_tests/gateway/test_cron_proactive_sync.py
tests/unit_tests/gateway/test_cron_scheduler.py
tests/unit_tests/gateway/test_document_attachments.py
tests/unit_tests/gateway/test_e2a_proxy.py
tests/unit_tests/gateway/test_enterprise_files.py
tests/unit_tests/gateway/test_lifecycle_handlers.py
tests/unit_tests/gateway/test_mcp_remote.py
tests/unit_tests/gateway/test_message_handler_cron_push.py
tests/unit_tests/gateway/test_message_handler_evolution.py
tests/unit_tests/gateway/test_message_handler_hook_params_update.py
tests/unit_tests/gateway/test_message_handler_mode.py
tests/unit_tests/gateway/test_message_handler_review_prompt.py
tests/unit_tests/gateway/test_message_handler_rewind.py
tests/unit_tests/gateway/test_message_handler_security_review_prompt.py
tests/unit_tests/gateway/test_message_handler_stream_cancel.py
tests/unit_tests/gateway/test_session_metadata_remote.py
tests/unit_tests/gateway/test_web_channel_orphan_routing.py
tests/unit_tests/gateway/test_web_http_auth_routes.py
tests/unit_tests/gateway/test_web_http_edition_mount.py
tests/unit_tests/gateway/test_web_media_normalizer.py
tests/unit_tests/runtime/test_cron_store.py
tests/unit_tests/runtime/test_resource_kernel.py
tests/unit_tests/runtime/test_runtime_service.py
tests/unit_tests/runtime/test_stream_interaction_answer.py
tests/unit_tests/runtime/test_tenant_request_scope_concurrency.py
tests/unit_tests/server/runtime/enterprise_config/test_apply_permissions.py
tests/unit_tests/server/test_archive_lifecycle.py
tests/unit_tests/server/test_enterprise_history_scope.py
tests/unit_tests/server/test_enterprise_http_tenant_scope.py
tests/unit_tests/server/test_enterprise_projectless_workspace.py
tests/unit_tests/server/test_front_control_tenant_bind.py
tests/unit_tests/server/test_gateway_adapter.py
tests/unit_tests/server/test_handler_tenant_manager_binding.py
tests/unit_tests/server/test_tenant_extract_from_http_query.py
tests/unit_tests/test_application_plugins.py
tests/unit_tests/test_desktop_electron_contract.py
tests/unit_tests/test_instance_manager.py
tests/unit_tests/test_model_config_null_recovery.py
tests/unit_tests/test_model_resource_fixes.py
tests/unit_tests/test_session_history_empty_filter.py
tests/unit_tests/test_web_http.py
```

</details>

## 历史首轮记录（已由上述修复和验证更新）


## 范围与状态

源：origin/release/1030 `13b737e3e7c8ac9d28663ff7864871c21c04b9d9`。
整合目标：upstream/develop `4e98ec676334d5e5254de72934a1467b8fbc5734`。
分支：feature/enterprise-integration。未修改 release 或 verify，未向远端推送或创建 PR。
本次固定上述主线版本，不将验证期间继续增长的 develop 自动纳入。
22 个文本冲突文件已经逐项处理。本候选尚未达到主线合入标准。

## 集成决策

- 保留 release 企业租户路由、定时任务、OBS 下载和版本开关；接入主线 Gateway 目录与公共模块迁移。
- 逐项修正迁移遗留导入，不批量采用 ours/theirs。
- ReqMethod 使用 openjiuwen_protocol 同一个枚举，沿用现有扩展机制补充 15 个企业方法；移除遮蔽共享枚举的本地副本。避免 A4P 请求相等性失效。
- 应用插件服务采用主线异步附件接口；兼容导出和视频扩展指向同一实现。其余协议 SDK 基类仍沿用 release 的 gateway_protocol，双包收敛不在本次擅自进行。
- 模型选择：个人版接入主线方法，企业版保留原租户模型选择；现有模型初始化仍有 release 遗留缺口，不能宣称主线模型功能完整。
- 恢复完整浏览器配置更新、素材引用渲染和组织身份逻辑；修正测试浏览器环境、真实 Gateway 路径和挂载桩。
- uv.lock 通过 uv lock 重新生成，补齐 gateway-protocol / minio 及传递依赖，保留主线 agent-core 固定版本和 croniter>=3。

## 验证证据

后端采用已有 Python 3.11 环境，PYTHONPATH 优先当前候选、锁定 agent-core b2a0a248c、openjiuwen-protocol 10a03dd、gateway-protocol 6edf9e3、A4P 1699ea3 和临时补齐依赖。
这不是干净锁定环境的验收。完整 uv sync 下载耗时过长，已停止；uv lock 已成功解析 327 包。
临时执行脚本：/private/tmp/enterprise-integration-pytest.py；完整日志：/private/tmp/enterprise-integration-evidence/。

| 测试 | release 基线 | integration |
|---|---|---|
| Gateway E2A、企业文件、cron、Web handlers、扩展管理、模型解析（424 项） | 396 通过 / 27 失败 / 1 跳过 | 404 通过 / 19 失败 / 1 跳过 |
| 协议转换、取消、HTTP 挂载、企业文件、OBS、模型准备（195 项） | 157 通过 / 18 失败 / 20 错误 | 首轮 164 通过 / 11 失败 / 20 错误；HTTP 挂载后续单独修复 3/3 通过 |
| 新增共享枚举、异步附件服务 | 未执行 | 7/7 通过 |
| 前端登录、A4P、协作快照 | 未执行 | 39/39 通过 |
| 前端历史分页 | 未执行 | 7/7 通过 |
| 前端素材引用 | 未执行 | 13/13 通过 |
| 前端组织身份渲染 | 未执行 | 修复测试环境与合并缺失逻辑后 3/3 通过 |

两轮后端对照中，integration 的失败/错误测试 ID 均包含于 release 基线的失败/错误集合；不等同于全量无回归。
未运行全量 UT、格式化、静态检查、生产构建、Docker/K8s、真实 LLM/数据库/OBS/Runtime 客户端联调。按仓库规则未执行格式化与静态检查。
未进行完整 617 文件企业增量的全面代码评审。

## 合入前待处理

1. Runtime foundation 依赖缺少 link_mtls_config，阻断企业 HTTP/路由相关测试。需要与客户镜像所用 Runtime 版本对齐。
2. release 已缺失 _RecipeModelCache 与部分主线模型初始化逻辑，13 个模型准备测试报错；AgentOS 测试引用缺失的 _third_agent_probe_port 导致收集失败。
3. release 已有 cron 终态/直播断言、CLI 安装接口、图片上传行为、专家取消测试和旧协议 chunk 类型问题。需按责任模块处理，不能用“原有失败”作为上游合入豁免。
4. 完成干净环境安装和真实个人版/企业版启动、升级回退、租户隔离与文件流式链路验证。
5. 上游 PR 前复核 develop 是否变化；若更新基线，重新运行受影响测试。

### 第一组未通过测试

```text
FAILED tests/unit_tests/gateway/test_e2a_proxy.py::test_proxy_preserves_authoritative_routing_metadata[ReqMethod.SESSION_LIST]
FAILED tests/unit_tests/gateway/test_e2a_proxy.py::test_proxy_preserves_authoritative_routing_metadata[ReqMethod.FILE_IMPORT_URL]
FAILED tests/unit_tests/gateway/test_cron_scheduler.py::TestCronLastSessionId::test_stream_completion_controls_last_session_id[empty-terminal]
FAILED tests/unit_tests/gateway/test_cron_scheduler.py::TestCronLastSessionId::test_stream_completion_controls_last_session_id[empty-final]
FAILED tests/unit_tests/gateway/test_cron_scheduler.py::test_stream_cron_publishes_thinking_and_deltas_on_the_live_bus
FAILED tests/unit_tests/gateway/test_app_web_handlers.py::test_dependency_ensure_force_reinstalls_when_bundled_cli_file_is_missing[claude-claude_agent_sdk-_install_claude_dependency_background-claude-dependency-install]
FAILED tests/unit_tests/gateway/test_app_web_handlers.py::test_dependency_ensure_force_reinstalls_when_bundled_cli_file_is_missing[codex-openai_codex-_run_codex_dependency_install_background-codex-dependency-install]
FAILED tests/unit_tests/gateway/test_app_web_handlers.py::test_build_optional_dependency_install_args_repair_mode_uses_force_reinstall
FAILED tests/unit_tests/gateway/test_app_web_handlers.py::test_optional_dependency_install_restates_openjiuwen_url_requirements
FAILED tests/unit_tests/gateway/test_app_web_handlers.py::test_upload_media_item_via_http_keeps_recognized_image_suffix[sample.jpeg-sample.jpeg]
FAILED tests/unit_tests/gateway/test_app_web_handlers.py::test_upload_media_item_via_http_keeps_recognized_image_suffix[sample.jfif-sample.jfif]
FAILED tests/unit_tests/gateway/test_app_web_handlers.py::test_upload_media_item_via_http_keeps_recognized_image_suffix[photo-photo.jpg]
FAILED tests/unit_tests/gateway/test_app_web_handlers.py::test_upload_media_item_via_http_keeps_recognized_image_suffix[notes.txt-notes.txt.jpg]
FAILED tests/unit_tests/gateway/test_app_web_handlers.py::test_external_cli_install_starts_managed_provider_runtime
FAILED tests/unit_tests/gateway/test_app_web_handlers.py::test_external_cli_install_skips_a_current_runtime
FAILED tests/unit_tests/gateway/test_app_web_handlers.py::test_external_cli_install_rejects_unknown_or_unmanaged_runtime[missing-True-BAD_REQUEST]
FAILED tests/unit_tests/gateway/test_app_web_handlers.py::test_external_cli_install_rejects_unknown_or_unmanaged_runtime[acme-False-UNSUPPORTED]
FAILED tests/unit_tests/gateway/test_app_web_handlers.py::test_external_cli_install_status_reports_managed_provider_runtime
FAILED tests/unit_tests/gateway/test_app_web_handlers.py::test_external_cli_custom_agent_remove_drops_config_before_uninstalling
```

### 扩展组剩余未通过测试（已排除修复通过的 HTTP 挂载）

```text
FAILED tests/unit_tests/e2a/test_gateway_normalize.py::test_non_organization_normalization_keeps_protocol_behavior[None]
FAILED tests/unit_tests/e2a/test_gateway_normalize.py::test_non_organization_normalization_keeps_protocol_behavior[payload1]
FAILED tests/unit_tests/e2a/test_gateway_normalize.py::test_non_organization_normalization_keeps_protocol_behavior[payload2]
FAILED tests/unit_tests/gateway/test_message_handler_stream_cancel.py::test_expert_stream_lifecycle_preserves_request_and_team_route[success]
FAILED tests/unit_tests/gateway/test_message_handler_stream_cancel.py::test_expert_stream_lifecycle_preserves_request_and_team_route[missing_terminal]
FAILED tests/unit_tests/gateway/test_message_handler_stream_cancel.py::test_expert_stream_lifecycle_preserves_request_and_team_route[error]
FAILED tests/unit_tests/gateway/test_message_handler_stream_cancel.py::test_expert_stream_lifecycle_preserves_request_and_team_route[cancel]
FAILED tests/unit_tests/gateway/test_message_handler_stream_cancel.py::test_expert_stream_lifecycle_preserves_request_and_team_route[other_team]
FAILED tests/unit_tests/gateway/test_message_handler_stream_cancel.py::test_expert_terminal_cannot_swallow_owner_cleanup_or_block_it
ERROR tests/unit_tests/agentserver/test_enterprise_file_http.py::test_http_persist_is_scoped_and_reuses_document_adapter
ERROR tests/unit_tests/agentserver/test_enterprise_file_http.py::test_http_session_list_uses_the_same_tenant_scope
ERROR tests/unit_tests/agentserver/test_enterprise_file_http.py::test_obs_import_strips_url_and_rejects_other_scope
ERROR tests/unit_tests/agentserver/test_enterprise_file_http.py::test_file_routes_registered
ERROR tests/unit_tests/agentserver/test_enterprise_file_http.py::test_import_reuses_quota_gate
ERROR tests/unit_tests/agentserver/test_enterprise_file_http.py::test_signed_download_is_bound_to_tenant
ERROR tests/unit_tests/agentserver/test_model_recipe_preparation.py::test_default_startup_avoids_unused_clients_and_alias_resolves_on_first_use
ERROR tests/unit_tests/agentserver/test_model_recipe_preparation.py::test_lazy_same_name_entries_preserve_global_positions_and_stable_id
ERROR tests/unit_tests/agentserver/test_model_recipe_preparation.py::test_custom_subagent_dictionary_lookup_materializes_its_selected_recipe
ERROR tests/unit_tests/agentserver/test_model_recipe_preparation.py::test_hot_reload_discards_stale_lazy_recipes
ERROR tests/unit_tests/agentserver/test_model_recipe_preparation.py::test_cold_selected_model_does_not_construct_default_provider[third-alias]
ERROR tests/unit_tests/agentserver/test_model_recipe_preparation.py::test_cold_selected_model_does_not_construct_default_provider[third#2]
ERROR tests/unit_tests/agentserver/test_model_recipe_preparation.py::test_cold_selected_model_does_not_construct_default_provider[model:third]
ERROR tests/unit_tests/agentserver/test_model_recipe_preparation.py::test_invalid_cold_selection_preserves_configured_fallback
ERROR tests/unit_tests/agentserver/test_model_recipe_preparation.py::test_default_group_does_not_construct_unused_single_model
ERROR tests/unit_tests/agentserver/test_model_recipe_preparation.py::test_invalid_selected_default_tries_next_valid_recipe
ERROR tests/unit_tests/agentserver/test_model_recipe_preparation.py::test_cold_typed_group_uses_real_request_before_default_construction
ERROR tests/unit_tests/agentserver/test_model_recipe_preparation.py::test_cold_typed_selection_rejects_access_before_any_provider_construction
ERROR tests/unit_tests/agentserver/test_model_recipe_preparation.py::test_duplicate_name_and_alias_skip_invalid_recipes_without_changing_index_identity
```

### 2026-10-28 A2UI 来源及局部主线回退复查

- `common/a2ui_web_config.py` 首次出现在 `ae186a1cb54936d87c78a31cdfda85493d0cca89`，Git 作者/提交者为 wangxin，两个父提交均不存在该文件。Gateway 移除本地 server provider 桥后，共享配置面板仍需读取 A2UI 字段，因此保留 common 辅助模块；补回主线配置解析的协议版本校验。实际 config.get 在未注册 A2UI provider 时通过回归。
- 以实际已合入主线 `b901e8cb6` 比较全部差异路径，并检查删除路径、旧文件匹配、Python 缺失函数及局部差异。`common/utils.py` 遗漏日志安全加载、64KiB 日志保护、邮箱正则性能修复、后台写日志、配置字段补齐及 common MCP manifest 引用；这些遗漏在 `d52c933d9` 已存在。恢复主线逻辑，保留企业租户路径和日志热更新，并使热更新同步队列内部 sink 和代理级别。
- 恢复测试误删的 portalocker 导入、staticmethod；去掉 COMMITTER 末尾换行和 model_routing_registry 文档翻译这两处无必要差异。未改其他文档作者、标题或索引。
- 回归：首轮相关 UT 198 passed；扩大 common + A2UI + utils + startup logging 为 1108 passed / 34 skipped / 1 failed，额外配置页回归 8 passed。唯一扩大回归失败为 test_external_cli_config_read 子进程将 PYTHONPATH 重置为仓库根目录，隔离测试依赖的 openjiuwen_protocol 不在该解释器安装环境中，报 ModuleNotFoundError；未修改测试绕过，不能宣称全绿。新增测试覆盖 48 组 A2UI 运行时/面板解析一致性及队列日志动态级别。
- fetch 后 upstream/develop 为 `4f0348f3e`，较 b901e8cb6 新增16个提交，尚未在本次局部修复中合入。这些属于后续同步，不作为历史遗漏删除相关文件。此次排查不等于对全部企业新增代码逐行语义验收；合入前仍需最新主线同步及独立完整环境回归。

### 2026-10-28 最新主线同步与提交整理

- 先快进保留远端团队 integration `25d56b315`（包含同步至 `396819cad`），再合入 upstream/develop `4f8eaaa78`，无文本冲突。
- 回归修复：鸿蒙测试重复导入、扩展加载器迁移引用、处理状态协议枚举；embedded 副本补回上传点目录保护和鸿蒙资源根路径；设计前端测试补浏览器/Vite 环境；片段排序 UT 隔离音轨与媒体探测外部依赖。
- 后端 design、gateway、a2ui、日志、配置迁移、utils 回归：最后大范围2358通过/2跳过/1失败；剩余视频合成排序测试补齐外部依赖隔离后，整个 test_clip_handler.py 11通过。设计前端14组共94通过。不是全仓或端到端验收。
- 用户明确要求合并杂乱提交：保留整理前历史备份，并以 `4f8eaaa78` 为父提交生成一个企业汇总提交；通过 Git tree 对比保证整理不改变代码内容。只改写 integration 分支，release/1030 与 enterprise-verify 不变。后续协作者须基于新 integration 继续，避免再次合入旧 integration 历史。
