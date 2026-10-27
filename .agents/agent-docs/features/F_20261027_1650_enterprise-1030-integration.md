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
  - jiuwenswarm/extensions
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/common/schema/message.py
  - gateway/extensions/sdk/application_plugin.py
  - tests/unit_tests/common/test_enterprise_protocol_identity.py
---

# 1030 集成候选记录

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
