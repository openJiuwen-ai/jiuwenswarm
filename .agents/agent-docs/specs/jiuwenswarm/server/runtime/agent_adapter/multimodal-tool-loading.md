---
title: Work / Code 多模态工具加载契约
author: null
author_note: 历史作者待确认；缺少原 owner 和可靠创建署名
id: S_01_multimodal-tool-loading
type: S
modules: []
files: []
---

# Work / Code 多模态工具加载契约

| 项 | 值 |
| --- | --- |
| 类型 | spec |
| 关联模块 | `jiuwenswarm/server/runtime/agent_adapter`、`jiuwenswarm/agents/harness/code`、`jiuwenswarm/agents/harness/common/tools` |
| 最近一次修订日期 | 2026-10-26 |
| 关联 feature | — |
| 修复依据 | [B_20261026_1602_code-multimodal-loading](../../../../../bugfix/jiuwenswarm/server/runtime/agent_adapter/B_20261026_1602_code-multimodal-loading.md) |

## 范围与配置来源

本规约定义 Work 和默认 Code 的应用层多模态工具装配及配置热更新。底层模型协议、媒体下载限制和工具输入 Schema 继续由现有工具实现负责。

`config.yaml` 的 `models.vision/audio/video` 是理解模型配置来源，接受 `model_config` 或 `model_client_config`。API key、地址、模型名与 provider 必须完整。能力开关依次读取 `VISION_ENABLED` / `AUDIO_ENABLED` / `VIDEO_ENABLED` 环境变量、模型块的 `enabled`；缺省时完整的旧模型配置视为启用。只有开关与配置检查同时通过，才装配对应工具组。解析及环境映射复用 `multimodal_config.py` 和适配器的模型配置构建方法。

生成能力保持 Work 原有配置条件：图片生成检查映射后的 `IMAGE_GEN_API_KEY`；视频生成和可视化生成分别检查现有 enabled / configured 判定。此规约不新增 provider 或模型支持。

## 工具集合与边界

| 能力 | 工具组 |
| --- | --- |
| 视觉理解 | `image_ocr`、`visual_question_answering` |
| 音频理解 | `audio_transcription`、`audio_question_answering`、`audio_metadata` |
| 视频理解 | `video_understanding` |
| 图片生成 | `generate_image` |
| 视频生成 | `generate_video`、`check_video_status` |
| 可视化生成 | `generate_visual` |

- **I-1：** 相同能力配置下，Work 与默认 Code 必须采用相同的工具组、构建条件及工具实现。提示词语言仍使用各模式既有规则。
- **I-2：** 默认 Code 的多模态工具不依赖 `modes.code.tools` 声明；旧列表中的多模态条目必须去重。该列表继续选择普通 Code 工具。
- **I-3：** 默认配置转换器生成的工具 bundle 通过 `include_multimodal=True` 启用公共装配。调用方自定义 bundle 缺省不启用；显式指定的多模态名称仍复用公共工厂。调用方自定义 Spec 不因配置热更新而扩展或替换工具集合。
- **I-4：** 所有装配路径保留已有工具 ownership 和 Agent 权限边界。视觉／音频实例使用 owner 作用域；共享工具仍使用共享注册，禁用单个会话的能力不得删除其他会话依赖的共享资源。

## 生命周期和失败行为

初始装配按能力组构建并注册。构建异常时记录警告并跳过该组；组内注册异常时还需清理本组已尝试的 owned 注册，其他组仍可装配。该降级行为不代表模型调用成功。

热更新复用相同工具组：启用后注册并加入能力管理器；禁用后移除本 Agent 的工具卡和能力；更新视觉／音频模型参数时保留已注册实例。默认 Code 同步维护 Spec build context 的实例记录和 `deep_config.tools`，供后续全量配置替换及回滚使用。

工具构建失败不得视为能力禁用。局部热更新保留失败组的当前实例、工具卡和注册记录，同时允许其他组正常更新；显式禁用能力时仍必须移除该组。

候选 Spec 构建不得提前替换适配器的当前多模态实例记录。默认 Code 全量替换时，多模态构建或注册异常必须中止候选并清理其已尝试的 owned 注册，包括失败前完成注册的其他组；再沿用 Code 既有回滚流程恢复旧配置、旧实例和旧注册。成功后才接管新实例。共享工具注册按进程共享语义保留。

## 验证索引

- 工具集合、旧列表兼容、定制 Spec、实际调用与组内失败：`tests/unit_tests/agentserver/test_code_adapter_multimodal_tools.py`。
- 开关热更新和实例参数刷新：`tests/unit_tests/agentserver/test_multimodal_capability_hot_reload.py`。
- Spec 全量重载与失败回滚：`tests/unit_tests/agentserver/test_code_spec_reload.py`。
- 配置解析和 ownership：`tests/unit_tests/agents/harness/test_multimodal_config.py`、`tests/unit_tests/agentserver/test_deep_adapter_tool_ownership.py`。
