---
id: B_20261116_1636_custom-model-reasoning-policy
title: 自定义模型误展示推理配置修复
type: B
author: lizhiyuan
author_email: lizhiyuan65@huawei.com
modules:
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/models
files:
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/ModelDialog.tsx
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/modelAdapters.ts
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/modelReasoning.ts
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/modelValidation.ts
  - jiuwenswarm/channels/web/frontend/tests/modelReasoning.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/modelSettings.test.mjs
designs: []
design_note: 用户已确认的既有设置页能力边界收敛；不新增模型发现或跨仓接口，属于局部回归修复。
---

# 自定义模型误展示推理配置修复

## 现象与根因

设置页把未命中厂商/模型静态规则的模型回退到 OpenAI 或 Anthropic 协议的通用推理档位。
因此手填厂商、模型与 API 地址的自定义模型，即使其端点不支持关闭推理，也会显示推理选择并允许保存无效配置。预置供应商的 `default_model` 和 `model_options` 未被复用时，切换供应商后模型名称也可能为空。

## 修复决策

- 自定义厂商与 OpenAI/Anthropic 协议无关：始终隐藏推理选择，切换到自定义时清空已有档位。
- 打开历史自定义模型时同样清空遗留档位；提交时再次强制输出空值，避免表单状态绕过 UI。
- 未命中任何预置厂商规则或全局模型族规则的非自定义模型也不展示推理选择；预置厂商、OpenAI Account 和已知模型族仍按 core 返回的静态规则展示。
- 供应商切换时使用服务端预置默认模型和模型列表，并只同步自动生成的展示名，避免保留上一供应商的模型名称。

服务端现有保存逻辑会把空 `reasoning_level` 从 `model_config_obj` 删除；Swarm 的运行时注入器因此不会构造 `reasoning`，由端点使用默认行为。

## 回归证据

- `npm run test:model-reasoning`：19/19 通过，包含自定义厂商历史推理值被清空且保存空值的 OpenAI / Anthropic 双协议回归。
- `npm run test:settings-panel`：68/68 通过，覆盖供应商默认模型、模型列表与模型草稿校验。
- `npm run build`：TypeScript 编译与 Vite 生产构建通过。
