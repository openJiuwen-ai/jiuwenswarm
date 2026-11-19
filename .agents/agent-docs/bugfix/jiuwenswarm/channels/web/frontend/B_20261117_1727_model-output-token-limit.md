---
id: B_20261117_1727_model-output-token-limit
title: 模型单次最大输出配置修复
type: B
author: lizhiyuan
author_email: lizhiyuan65@huawei.com
modules:
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/models
  - jiuwenswarm/common/config_panel
files:
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/ModelDialog.tsx
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/ModelsSettings.tsx
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/OutputTokenField.tsx
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/OutputTokenField.css
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/outputTokens.ts
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/modelAdapters.ts
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/modelValidation.ts
  - jiuwenswarm/channels/web/frontend/src/features/settings/services/settingsContract.ts
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json
  - jiuwenswarm/channels/web/frontend/src/types/index.ts
  - jiuwenswarm/common/config.py
  - jiuwenswarm/common/config_panel/models_handlers.py
  - jiuwenswarm/channels/web/frontend/tests/modelSettings.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/settingsRefactor.test.mjs
  - tests/unit_tests/common/test_config_validate_model_mcc.py
  - tests/unit_tests/gateway/test_app_web_handlers.py
  - tests/unit_tests/test_model_resource_fixes.py
designs: []
design_note: 用户在本轮会话确认的模型设置范围调整，未形成独立设计文档。
---

# 模型单次最大输出配置修复

## 现象与影响

模型设置页仅支持上下文窗口，用户无法为一次调用限制输出 token。Anthropic 端点达到输出上限时，原有默认值也无法由用户发现或调整；保存后，前端列表不会回显该配置，且 OpenAI 的“未限制”语义无法显式清除旧值。

## 修复决策

- 在模型编辑弹窗中，沿用上下文窗口的布局新增“模型单次最大输出”，提供 8K、16K、32K、64K 预设和自定义输入。
- Anthropic 默认使用 32K；已知低上限 Claude 模型按照可支持上限限制可选值。OpenAI 协议额外提供“不限”，其提交语义为不传输出参数。
- 前端将“不限”编码为 `null`，后端在资源更新时删除已保存的 `max_tokens`；字段省略则保持历史配置不变，避免编辑其他字段时意外迁移旧模型。
- 资源列表与详情回传有效的正整数 `max_tokens`，使保存后的配置可回显。运行期仍复用 Core 的现有模型错误事件，Anthropic 输出耗尽将由 Core 转成可展示异常。
- “测试连接”使用保存前草稿的 `max_tokens`：数值覆盖旧配置，`null` 清除旧配置；Anthropic 思考预算不小于已配置上限时，在探测前返回与运行期一致的配置错误，避免保存后首次对话才失败。

## 回归证据

- `npm run test:settings-panel`：70/70 通过，覆盖 OpenAI 不限、Anthropic 默认值、已知低上限模型及弹窗布局。
- `.venv\Scripts\python.exe -m pytest tests\unit_tests\test_model_resource_fixes.py -q`：58/58 通过，覆盖资源读写及清除旧 `max_tokens`。
- `.venv\Scripts\python.exe -m pytest tests\unit_tests\common\test_config_validate_model_mcc.py tests\unit_tests\gateway\test_app_web_handlers.py::test_config_validate_model_retries_failed_probe_with_more_tokens tests\unit_tests\gateway\test_app_web_handlers.py::test_config_validate_model_rejects_anthropic_limit_below_thinking_budget -q`：35/35 通过，覆盖草稿上限覆盖/清除和 Anthropic 思考预算冲突的保存前错误。
- `npm run build` 到达 TypeScript 编译，但仓库现有前端依赖中缺少 `oidc-client-ts`，在 `src/auth/manager/federation.ts` 停止；未修改共享依赖目录规避该环境问题。

## 遗留与后续

无。新增供应商或模型族的上限规则时，需要同步前端 `outputTokens.ts` 与 Core 的模型能力规则。
