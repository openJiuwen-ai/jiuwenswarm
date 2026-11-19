---
id: F_20261027_1843_ttse-plugin-rail
title: TTSE 以插件 Rail 接入主 Coding Agent
type: F
author: xinyang_bali
author_email: 835397263@qq.com
modules:
  - jiuwenswarm/server/runtime/agent_adapter
  - jiuwenswarm/agents/harness
  - jiuwenswarm/extensions/sdk
  - jiuwenswarm/resources/plugins/ttse
files:
  - jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_code.py
  - jiuwenswarm/extensions/sdk/plugin_runtime.py
  - jiuwenswarm/agents/harness/agent_observability.py
  - jiuwenswarm/resources/plugins/ttse/manifest.json
  - scripts/package_ttse_plugin.py
designs: []
design_note: 用户确认执行 D:/files/自演进/方案/ttse适配code agent 中的外部适配方案，该资料没有仓内 D 标识。
---

# TTSE 以插件 Rail 接入主 Coding Agent

日期：2026-10-27。当前契约：[S_02_ttse-plugin-runtime](../specs/jiuwenswarm/server/runtime/agent_adapter/S_02_ttse-plugin-runtime.md)。
跨仓部分位于用户指定的 `D:/codes/自演进研究/agent-core`，归档为 `F_20261027_1810_ttse-plugin-runtime`。

## 背景与实际决策

MCP Manager 的 file/class Rail 可以零参装配，但 TTSE 依赖真实模型、轨迹与数据目录。
本次新增会话运行服务、进程轨迹需求和 TTSEBinding，沿用 core 的统一文件加载器与回调。work/agent 保留内置 TTSE；code 最终采用仅插件方式。
包保存在 `jiuwenswarm/resources/plugins/ttse`，用 `scripts/package_ttse_plugin.py` 生成可复现 ZIP。

包内 config.yaml 提供业务默认值，运行依赖只在 manifest 声明。主模型和凭据由宿主传递，避免另一套密钥配置和全局可变模型。
已创建 Agent 的冷启动装配可能没有观测 Rail，因此插件激活还会补挂宿主拥有的观测 Rail；退出时保留原有观测 Rail。
旧版 TTSE 清理不再提前清空活任务集合，否则插件接管时可能丢失旧写入者。
失败升级从挂载时的临时不可变包快照恢复，避免磁盘上原路径已经被坏版本覆盖。

## 对设计的实施调整

2026-10-28 用户进一步要求移除 code 的非插件 TTSE 注入。因此移除 code 固定 Rail 构建、旧开关装配、动态配置 builder 和卸载后恢复内置的路径；阻断继承自 work 的内置 builder。旧开关及业务参数仍供 work 使用，code 插件可沿用业务参数但忽略其 enabled 开关。这一要求替代外部方案中 code 内置/插件兼容切换的部分。

原方案建议到期强制取消并确认退出。核验发现经验库存储经 `asyncio.to_thread` 写盘，取消 asyncio awaiter 不能证明线程停止。
因此本次选择关闭门控、总等待预算和宽限预算，超时继续看护真实任务，不强制取消含线程 I/O 的任务。
这保留了“旧写入者退出后才能接管”的约束；可能需要等待 core 自身的模型超时到期。超时失败的装备变更不持久化新选择。

## 拒绝的方案

- 插件直接改写全局 observability.enabled：卸载无法区分其他会话/功能的需求。
- 依赖进程粘性的 force=True：无法随会话需求回收。
- 仅安装 MCP 查询工具：没有轨迹、自动归纳和生命周期闭环。
- 只使用类名查重：不同插件可以使用不同类名，仍可能重复归纳同一 TTSE 能力。
- 直接把原包路径作为失败恢复来源：同路径升级已覆盖旧代码，无法可靠回滚。

## 验证

开发中使用本地 core 源码与现有 `.venv`，未改写已确认的官方 core 发布依赖锁。
真实模型调用验证使用可控客户端替代网络，保留 Model/回调/provider/run span/Runner/文件 Rail 的真实执行链；确认首轮 consult 可见并有非空轨迹。
2026-10-28 最终宿主回归：**416 passed**。文件集为：

```text
tests/unit_tests/server/extensions/test_chat_send_extension_mount.py
tests/unit_tests/server/extensions/test_ttse_plugin_package.py
tests/unit_tests/server/extensions/test_extension_package_manager.py
tests/unit_tests/agentserver/test_ttse_plugin_runtime.py
tests/unit_tests/agentserver/test_code_adapter_ttse_rail.py
tests/unit_tests/agentserver/test_agentserver_modes.py
tests/unit_tests/agentserver/test_deep_adapter_session_teardown.py
tests/unit_tests/agentserver/test_trajectory_runtime_wiring.py
tests/unit_tests/agentserver/test_debug_trace.py
tests/unit_tests/agentserver/test_code_spec_reload.py
tests/unit_tests/agentserver/test_code_adapter_multimodal_tools.py
tests/unit_tests/rsi/test_plugin_roundtrip.py
```

命令为现有 `.venv/Scripts/python.exe -m pytest -o addopts='' -o log_cli=false --asyncio-mode=auto -q --tb=short --show-capture=no <上述文件集>`，设置 PYTHONUTF8=1，PYTHONPATH 包含当前仓库、本地 ../agent-core 和 `.git/codex-merge-verify-deps`。首次未开启 UTF-8 时，一个既有中文 JSON 测试因默认 GBK 解码失败；UTF-8 重验全部通过，未改写该测试。

core 扩大回归 **211 passed**，见跨仓 F_20261027_1810_ttse-plugin-runtime。当前宿主依赖未直接安装到全局环境；验证缺失的协议/存储/Redis 依赖补齐在 `.git/codex-merge-verify-deps`。没有调用外部真实模型或 Redis 服务。

插件 ZIP 已经由真实 import_plugin_package → install_plugin_package 验证，并确认可复现字节输出。输出位于 `D:/files/自演进/方案/ttse适配code agent/ttse_1.0.0.zip`。
扩大回归后又补强了 team member 实例标识的拒绝检查，并定向重验 TTSE 插件及 code 非插件禁用用例：**24 passed**。升级预检后的版本漂移不会绕过 TTSE 收尾，候选和旧版本恢复同时失败时两份租约均释放。

## 交付与限制

### 2026-10-29 收敛为原生插件通用机制

按用户“把宿主收敛成通用机制”的要求，将原 TTSE 专用 SDK/manager 删除，替换为 extensions/sdk/plugin_runtime.py 和 server/runtime/agent_adapter/plugin_runtime.py。适配器只接通用装备事务、卸载及 cleanup；插件资源不再共享 adapter._ttse_rail 内置槽位，相应 TTSE 专用守卫也移除。work 内置 TTSE 保持原业务路径。

通用契约使用 metadata.plugin_runtime v1，支持主 code/work、声明式 main_model/trajectory/agent_data_workspace/embedding_factory、业务无关的互斥能力名及 direct_tools。每个插件有独立绑定、快照和租约，多插件可共存。未知服务/能力冲突在获取租约前拒绝；包内同步 prepare_plugin_context 完成业务预检；from_plugin_context 构造并 register_resource。宿主 SDK/管理器不包含 TTSE ID、选项、bank schema、归纳策略或算法。

TTSE 包升为 2.0.0，选项、旧配置兼容、bank 校验、embedding 及 LLMInvokePolicy 均在 ZIP 内。旧 coding_runtime v1/v2 明确拒绝，需配套升级宿主和插件。不新增 core API，不自动更新依赖或修改正在运行的服务。

通用关闭协议给所有 closer 一个总预算并保留实际 Task，超时不取消，重试复用仍运行的 closer。模型对象或配置改变也受 fresh-turn 限制。同批运行插件失败时恢复旧绑定；不宣称任意普通插件副作用/业务文件可全局回滚。新服务及 team/子 agent 作用域仍需扩展宿主，既有服务内新增插件只需 ZIP。

新增独立 notes/audit 和普通零参 Rail 验证：与 TTSE 同时运行、声明服务隔离、capability 冲突、两个轨迹租约独立释放、跨插件失败恢复、work 主 agent、模型对象替换、超时不取消/不重复 closer、配置变化 fresh-turn。包测试从真实 ZIP 解包经 core loader 加载，首轮真实 Model/Runner/span/工具可见性回归保留。

当前 `.venv`、PYTHONUTF8=1、PYTHONPATH=本仓（不加入本地 agent-core）下最终扩大回归 **443 passed**，涵盖 TTSE/package、code TTSE、模式、session teardown、trajectory wiring、装备装卸、debug trace、配置重载、多模态和 RSI roundtrip。新增类名冲突场景，确认候选清理不误注销其他插件的同名实例，并清除恢复前的旧加载记录。使用 uv sync 安装的 core；未调用真实外部模型完成业务任务。本节替代此前的宿主 TTSE 专用 runtime v1/v2 方案，当前契约见 S_03_native-plugin-runtime 和更新后的 S_02。历史验证与旧 ZIP hash 仅代表当时产物。

当前产物为 `D:/files/自演进/方案/ttse适配code agent/ttse_2.0.0.zip`，SHA256 `5E8E994AF972D55819E5E9CD77EBAF3BB7E66A1A54D90043E8BE60B28FD68729`。四个归档文件均与源码逐字节一致；外部接口示例也与当前 SDK 和 Rail 源码一致。本次未替换用户已安装插件、未重启服务、未提交或推送代码。

### 2026-10-29 删除 core 工厂能力版本常量

按用户要求删除本地 core 的 FILE_RAIL_CONTEXT_FACTORY_VERSION，宿主同步删除对应检查与只为该检查存在的 meta_elements 导入。不增加替代标志，不通过源码字符串判断能力；由真实文件 provider 工厂调用及 AgentRail 返回值校验完成构造。普通无工厂 Rail 继续执行 rail_cls(**params)，TTSE 工厂仍消费 runtime v2。宿主 runtime 版本负责服务字段兼容，与本次删除的 core 能力标志不同，继续保留。

代价是旧 core 的不兼容错误不再提前在候选预检时报告，而在实际插件加载时产生，并经过已有候选清理和旧绑定恢复路径。现有宿主 `.venv`、PYTHONUTF8=1、PYTHONPATH=本仓;../agent-core 下，TTSE runtime/package/chat-send 装备入口 **84 passed**；同一解释器下 core resources/manifest **40 passed**。core 和宿主文档均已同步。此前本节上方/下方有关工厂版本探测的旧描述不再适用。

### 2026-10-28 插件独立装配

用户确认将 TTSEConfig 构建和 TTSERail 实例化移入 ZIP，并删除宿主冗余代码。`rails/ttse_plugin_rail.py` 现在消费依赖快照、自行构建 TTSEConfig 和 cls 实例，返回前登记 constructed_rails。宿主删除 build_ttse_rail 及未被使用的 resolve_ttse_options，不再导入或构造 code 插件的 TTSEConfig/TTSERail；保留模型快照、只读选项、bank 预检、轨迹租约、回滚和关闭看护，因为这些仍承担加载前校验与资源生命周期职责。work 内置 TTSE 构建不属于本次删除范围。

插件版本更新为 **1.1.0**。删除旧工厂服务是运行接口变更，coding runtime 升至 **v2**，新宿主在预检拒绝旧 v1 包，避免释放旧 owner 后才触发 AttributeError。core 文件工厂仍为 v1，本次没有 core API 改动。升级需要配套导入安装新 ZIP 和重启宿主；当前已加载的旧包不会被源码打包自动升级。

验证 fixture 改为生成真实 ZIP 再解包，经宿主装备入口及 core 文件 provider 加载。新增证据包括：runtime 已无宿主构建方法，修改 ZIP 工厂策略可以改变实际 TTSEConfig，包/legacy/宿主配置覆盖仍生效，模型及轨迹对象身份保持一致，旧契约预检失败不替换活跃实例。既有首轮真实 span、consult/经验持久化、半初始化失败及关闭超时测试继续覆盖迁移后链路。

当前 `.venv`、PYTHONUTF8=1、PYTHONPATH=本仓;../agent-core 下，TTSE runtime/package、code TTSE、agentserver modes、session teardown、trajectory wiring、chat-send 装备入口、extension package manager 共 **331 passed**。保留本地 core 测试环境说明，不据此宣称纯锁定依赖或在线真实模型已重验。本次只修改实现、文档和交付包，未自动替换用户已安装插件或重启运行服务。

产物为 `D:/files/自演进/方案/ttse适配code agent/ttse_1.1.0.zip`，SHA256：`79ACCB3420EB20A3E74491B45327FB1CE4516FB6BD6F80B16C984ADA675B0DF7`。旧 1.0.0 ZIP 保留为历史版本。当前契约以 S_02 为准，上文关于工厂位于宿主和 runtime v1 的初版记录由本节替代。

### 2026-10-28 Web canonical 模式误拒绝修复

实际 Web 会话使用 `agent.code.normal`，原运行时只接受 `mode=code`，导致主 coding agent 被误拒绝。改用 `resolve_request_mode` 和既有 `resolve_agent_request_mode`，按 code profile / 非 team 判断；继续拒绝显式 team、team 子模式和实际 member 实例。新增正常/plan、新旧别名及 Web 组合的真实插件装卸回归，另验证 team 入口跳过装备且直接挂载事务拒绝。

现有 `.venv`、PYTHONUTF8=1、PYTHONPATH=本仓;../agent-core 下重验 TTSE runtime、code TTSE、agentserver modes、chat_send_extension_mount：**167 passed**。此次已安装 fork develop 的 `74a40ce2f7316598e6889c17b83eeb15e4852f63` 缺少 file Rail context factory v1；本次服务进程通过 PYTHONPATH 使用之前修改的本地 core，保留 pyproject/uv.lock 的 fork develop 来源。以上不证明锁定 fork 单独具备插件运行能力。

首版 code/main、单进程写同一 bank；无新增前端 UI。config/model 更新在下一装备边界生效。
生产发布前需提交并发布本次 core 改动，再将宿主 pin 更新到包含工厂 v1 与 TTSE aclose 的版本。
本次不自动提交、推送、安装用户工作区插件或调用真实外部模型。

### 2026-10-30 rebase 冲突与配套包修复

将 dc0154106 重放至 d83cfbb8e 时合并三个冲突块：agent_observability 同时保留上游根 span 所需导入及插件租约导入；interface_deep 的装备变更保留通用 runtime transition，并保留上游 AgentError/OSError/RsiError 错误响应；卸载同时处理 RSI Harness 选择与通用 runtime 绑定。未使用整文件 ours/theirs 覆盖任一侧。

验证发现待重放提交中的 TTSE manifest/工厂/README/config 仍为 1.1.0 coding_runtime v2，与同提交的通用 runtime v1、S_02 及测试不一致。恢复为此前已验证并安装的 TTSE 2.0.0 配套源码，使用 prepare_plugin_context、runtime.require/register_resource 和 embedding_factory；bank 继续为 agent_data_workspace/.ttse/code/bank.json，不恢复已撤销的专用目录改动。

使用现有 .venv 和本地 ../agent-core，另将新基线 pin 的 openjiuwen-protocol 2a0457e5e01152e9ca5f8afdffd84716227046ca 安装到 .git/ttse-rebase-verify-deps，仅通过测试 PYTHONPATH 加载，不改 .venv、pyproject 或 uv.lock。定向 TTSE runtime/package、轨迹、RSI 会话切换/roundtrip、chat-send 装备验证共 **114 passed, 26 skipped**；跳过项均为 chat-send fixture 缺少 gateway_protocol 的既有导入门控，不能视为已验证。单独轨迹验证 **14 passed**；首轮使用旧协议包收集失败，补齐基线 pin 后才完成上述运行。未运行格式化或静态检查，未调用真实外部模型。

### 2026-10-30 撤销 TTSE 专用目录切换

按用户最新要求撤销本次路径修改，code TTSE 恢复 `agent_data_workspace/.ttse/code/bank.json`，当前 default 实例为 `<数据根>/agent/workspace/.ttse/code/bank.json`。删除此次新增的 get_jiuwenclaw_workspace_dir helper 和专用目录测试，恢复预检、构建、中文/英文说明及 S_02 契约。其他数据工作区不变；新旧两份 bank 均保留，不合并、不移动、不覆盖。

真实 ZIP 装备/卸载/回滚及包导入测试在 `.venv`、PYTHONUTF8=1、PYTHONPATH=本仓;../agent-core、asyncio_mode=auto 下 **48 passed**。重新生成 ttse_2.0.0.zip，并同步当前已安装 TTSE 的 Rail 和 README 后重启 default 实例，以清除进程内的旧路径绑定。此撤销替代本次临时目录切换，不影响先前插件通用运行时改动；未提交或推送，未调用真实外部模型。
