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

沿用 `memory.builtin` 配置，通过 `JiuwenMemoryCodingRail` 接入 SDK/HTTP 文档记忆。Swarm 负责配置、项目与身份，core 负责回调，agent-memory 负责抽取、文档及检索。仓库模板默认启用 jiuwen，显式空 provider 或旧配置缺少该字段时保留原生记忆；选中 jiuwen 后初始化失败不回退旧编码记忆。

现行规则由 [Team 接入及统一身份决策](F_20261118_1150_team-document-memory.md) 更新：Work/Code/Team 统一使用配置 agent_id，缺省 main_agent；session 保留配置值。共用文档根不取消身份过滤。同一用户、空间及 Agent 身份可共享画像，Code 项目按规范路径生成的键隔离，模型不能改写该键。

主 Agent 与 code_agent 共用新 Rail/Provider，各自管理工具与召回上下文。每轮 sync_turn 与旧 AutoMemory 独立运行（后者仍受原开关控制）；ProjectMemoryRail 仍读取项目说明书。Coding write/edit 工具沿用 MemoryForbiddenRail 的内容限制。

HTTP 只创建客户端，Runtime、文档与看门狗由服务端持有。HTTP 记忆指纹忽略本地模型、SDK 文档根和 agent 工作区；身份、项目或连接配置变化才替换客户端。完整 Spec 重载复用 Rail 时，在异步初始化前重新绑定工具及提示词，避免临时卸载触发 Provider 关闭。

SDK 兼容文档存储配置各层的 YAML 空值，非法类型明确报错。Jiuwen 编码 Rail 构造或重建失败即清空槽位并关闭旧实例，不退回原生 Rail；ProjectMemoryRail 独立保留。原生路线的同绑定失败保留策略及目录指纹不变。

## 取舍与边界

初版按运行 Agent 名称隔离的取舍已被上述统一配置身份方案替代；运行名称不再作为 Jiuwen 记忆的隔离依据。AutoMemory 仍写旧目录，不继承 Jiuwen Rail 或工具；与 sync_turn 同时开启会各自抽取，可能增加模型调用，不合并两套结果。不扩展 Team Code、Dreaming、旧主题文件迁移、多 runtime 并发治理或总 token 配额。

正式使用依赖包含本特性的 core 和 agent-memory 发布物，发布后再更新依赖版本与 lock；本次不预填未发布版本。

## 验证基线

Swarm 装配单测使用外部 Rail/Provider 替身，验证 SDK/HTTP 参数、身份及项目绑定、工具限制、开关切换、Spec 重载顺序、Jiuwen 重建失败清空及 AutoMemory 原开关触发，不要求预装未发布的跨仓接口。Provider 持久化、回调及资源释放分别由 agent-memory 和 core 的测试覆盖；替身测试不代替依赖版本更新后的真实接入验证。

## 2026-11-11 评审修订

此前为减少重复抽取而跳过旧 AutoMemory，并为可用性保留同绑定下重建失败的旧 Rail。此次按原设计替换这两项决定：恢复 AutoMemory 原有触发规则；仅 Jiuwen 路线改为构建失败清空编码槽位。SDK/HTTP 选择、身份和项目隔离、原生 Coding 与 Dreaming 行为不变。模板及中英文使用说明补齐配置、存储和失败行为；依赖版本更新与多 runtime 并发治理另行处理。


## 2026-11-16 场景装配重构

按已确认方案，将 Work/Coding 的 Rail 导入、Provider 参数、Rail 类型及专属参数选择收敛到场景工厂注册表。公共函数继续处理配置、身份与 SDK/HTTP 装配，Core Rail 无业务修改；agent-memory 独立抽取记忆行为策略。

保留按场景延迟导入，Work 不依赖 Coding Rail 可用；注册表保存工厂而非实例，防止不同项目或模式共享可变参数。没有增加动态插件加载或改动上层模式选择，后续接入仅扩展工厂及对应记忆策略。

装配与 Coding 回归 69 项通过，覆盖 SDK/HTTP、项目与身份、热更新、构建失败停用、新场景注册、实例独立及缺少 Coding Rail 时 Work 可用。此处使用依赖替身；Memory 文档和 HTTP 回归 124 项、Core 两类 Rail 回归 25 项通过。另以实际 Swarm 工厂、Core Rail 和 Memory SDK 验证并行初始化、交错写入和关闭一方后继续使用，数据写入临时目录。未执行真实 LLM 页面回放，未更新发布依赖。


## 2026-11-18 上游增强能力合并

保留场景工厂入口，Work 工厂选用上游增强版 SwarmJiuwenMemoryRail；Coding 继续使用专属 Rail。两种模式统一按规范项目路径生成摘要键，在同一记忆库且身份允许时共享同项目内容，不再依靠不同模式的项目键差异隔离。不同路径的项目隔离保留；Agent 维度现按配置身份隔离。

公共装配保留上游默认项目、原始 MEMORY.md 模板归档、媒体配置与获准目录透传。热更新同时检测媒体配置和配置中的记忆身份；模型重载继续保留当前选中的模型。索引沿用上游稳定生成规则，仅增加本功能条目。

本轮定向回归 222 项通过，覆盖 SDK/HTTP 装配、同项目键、同名不同目录隔离、Agent 身份、媒体权限与热更新、模板归档、模式切换、模型重载及无项目工作区。Core、e2a 和 Gateway 使用当前 Swarm 锁定版本的临时源码环境；Coding 装配沿用边界替身，Memory 包使用本地实现。未重跑真实 LLM 页面验收，未修改发布依赖。


## 2026-11-19 默认配置对齐

沿用上游模板的 `provider=jiuwen`、本地 hashing 向量与 64 维默认值，并同步中英文使用说明及 S04。显式空 provider 仍选择原生记忆；已有用户配置独立加载，不被模板覆盖，可继续指定真实 embedding 模型和对应维度。保留上游工具失败状态传播、增强版 Work Rail 和 Coding 场景工厂。

本轮定向回归 191 项通过，覆盖 SDK/HTTP 装配、身份和项目绑定、热更新、工具错误传播及会话能力。Core、Gateway 和 e2a 使用当前 Swarm 锁定提交的临时源码，Memory 使用本地实现；Coding 边界装配沿用测试替身，未重跑真实 LLM 页面验收，未更新发布依赖。
