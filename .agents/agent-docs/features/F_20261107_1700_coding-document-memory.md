---
author: Wang Tao
author_email: zzyyww0808@gmail.com
designs: []
design_note: 依据已确认的跨仓 Coding 文档记忆方案；无独立仓内 D 文档。
id: F-20261107-1700-coding-document-memory
title: Coding 接入 agent-memory 文档记忆
type: F
modules: [jiuwenswarm/server/runtime/agent_adapter, jiuwenswarm/agents/harness/common/memory, jiuwenswarm/agents/harness/common/rails]
files: [jiuwenswarm/server/runtime/agent_adapter/interface_deep.py, jiuwenswarm/server/runtime/agent_adapter/interface_code.py, jiuwenswarm/server/runtime/agent_adapter/interface.py, jiuwenswarm/agents/harness/common/memory/jiuwen_rail_builder.py, jiuwenswarm/agents/harness/common/rails/memory_forbidden_rail.py, jiuwenswarm/agents/harness/common/auto_memory/extraction_runner.py]
---

# Coding 接入 agent-memory

日期：2026-11-07。配置与调用契约见 [S04](../specs/jiuwenswarm/server/runtime/agent_adapter/S_04_coding-document-memory.md)。

## 功能与决策

沿用 `memory.builtin` 配置，通过 `JiuwenMemoryCodingRail` 接入 SDK/HTTP 文档记忆。Swarm 负责配置、项目与身份，core 负责回调，agent-memory 负责抽取、文档及检索。默认不启用 jiuwen；选中后初始化失败不回退旧编码记忆。

Work/Code 均优先使用运行 Agent 名称，缺省才使用配置 agent_id；session 保留配置值。共用文档根不取消身份过滤。同一用户、空间及 Agent 身份可共享画像，Code 项目按规范路径生成的键隔离，模型不能改写该键。

主 Agent 与 code_agent 共用新 Rail/Provider，各自管理工具与召回上下文。每轮 sync_turn 与旧 AutoMemory 独立运行（后者仍受原开关控制）；ProjectMemoryRail 仍读取项目说明书。Coding write/edit 工具沿用 MemoryForbiddenRail 的内容限制。

HTTP 只创建客户端，Runtime、文档与看门狗由服务端持有。HTTP 记忆指纹忽略本地模型、SDK 文档根和 agent 工作区；身份、项目或连接配置变化才替换客户端。完整 Spec 重载复用 Rail 时，在异步初始化前重新绑定工具及提示词，避免临时卸载触发 Provider 关闭。

SDK 兼容文档存储配置各层的 YAML 空值，非法类型明确报错。Jiuwen 编码 Rail 构造或重建失败即清空槽位并关闭旧实例，不退回原生 Rail；ProjectMemoryRail 独立保留。原生路线的同绑定失败保留策略及目录指纹不变。

## 取舍与边界

不固定所有 Agent 为同一身份，以保留 Agent 查询隔离。AutoMemory 仍写旧目录，不继承 Jiuwen Rail 或工具；与 sync_turn 同时开启会各自抽取，可能增加模型调用，不合并两套结果。不扩展 Team Code、Dreaming、旧主题文件迁移、多 runtime 并发治理或总 token 配额。

正式使用依赖包含本特性的 core 和 agent-memory 发布物，发布后再更新依赖版本与 lock；本次不预填未发布版本。

## 验证基线

Swarm 装配单测使用外部 Rail/Provider 替身，验证 SDK/HTTP 参数、身份及项目绑定、工具限制、开关切换、Spec 重载顺序、Jiuwen 重建失败清空及 AutoMemory 原开关触发，不要求预装未发布的跨仓接口。Provider 持久化、回调及资源释放分别由 agent-memory 和 core 的测试覆盖；替身测试不代替依赖版本更新后的真实接入验证。

## 2026-11-11 评审修订

此前为减少重复抽取而跳过旧 AutoMemory，并为可用性保留同绑定下重建失败的旧 Rail。此次按原设计替换这两项决定：恢复 AutoMemory 原有触发规则；仅 Jiuwen 路线改为构建失败清空编码槽位。SDK/HTTP 选择、身份和项目隔离、原生 Coding 与 Dreaming 行为不变。模板及中英文使用说明补齐配置、存储和失败行为；依赖版本更新与多 runtime 并发治理另行处理。
