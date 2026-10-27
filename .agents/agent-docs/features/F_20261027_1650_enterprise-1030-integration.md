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
