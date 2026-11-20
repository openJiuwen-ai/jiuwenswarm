---
id: S_03_native-plugin-runtime
title: 原生插件通用运行服务与生命周期
type: S
author: xinyang_bali
author_email: 835397263@qq.com
modules:
  - jiuwenswarm/server/runtime/agent_adapter
  - jiuwenswarm/extensions/sdk
files:
  - jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py
  - jiuwenswarm/extensions/sdk/plugin_runtime.py
---

# 原生插件通用运行契约

## 声明与服务

metadata.plugin_runtime 声明 contract_version=1、profiles（code/work 非空子集）、role_scope=main、requires、capabilities、direct_tools。旧 metadata.coding_runtime 拒绝并提示升级；无运行声明的普通插件沿用原路径。目前不开放 team、子 agent 或外部 Harness。

capabilities 为插件自定义的互斥能力名，目标集合中不得重复；宿主不内置业务名。不同插件同时挂载，各自持有绑定、包快照和租约。direct_tools 指定标为 DIRECT 的已存在工具，缺失工具不强行创建。

宿主仅提供 requires 声明的服务；未知服务在获取任何租约前拒绝。runtime.require 对未声明或不可用服务报错。

| 服务 | 值 |
|---|---|
| main_model | 同一快照的模型对象/模型名二元组 |
| trajectory | 真实共享 TrajectorySpanProcessor，每个使用者持有独立租约 |
| agent_data_workspace | agent 数据根绝对 Path，不使用用户项目目录 |
| embedding_factory | OpenAI 兼容 embedding provider 构造能力，业务参数由插件决定 |

PluginRuntimeServices 包含身份、profile、只读 host_config/package_config/options、services 和可变 resources 跟踪列表。对象不序列化或记录配置凭据，服务对象保持原身份。

## 预检和构造

适配器通过公开 `get_plugin_runtime_host()` 提供当前 `PluginRuntimeHost` 快照，包含实例、插件登记映射、成对模型、工作区、profile、配置与 fresh-turn 回调。快照每次读取重新构造，模型/配置变化不被缓存；插件登记映射保持同一对象以反映装卸。该快照不输出含凭据的 repr。管理器通过适配器公开 `get_plugin_runtime_manager()` 获取单实例，不直接访问适配器受保护字段。槽位经管理器公开 transition_lock 协调恢复，挂载、卸载、看护及依赖释放由显式槽位操作完成。

运行包至少声明一个文件 Rail。宿主检查安装/连接、fresh-turn、包路径、profile/role 和服务，再调用各文件类的可选同步 prepare_plugin_context(runtime)。预检只做配置解析和必要的数据读取，不启动任务、注册工具或写业务数据，返回普通 dict 作为有效 options。

包默认值来自 config.yaml，宿主覆盖来自 plugins[manifest.id]；字段、业务校验和兼容优先级由插件实现。候选先完成预检，再获取轨迹租约和包快照、关闭旧绑定。工厂通过 context.extras.plugin_runtime 取得最终服务，返回前 register_resource 登记构造的 Rails；宿主核实这些实例实际已注册。

trajectory 在申请租约后才可用，prepare_plugin_context 不得请求该服务；main_model、agent_data_workspace 和 embedding_factory 可用于业务预检。

线程获取租约和复制包快照期间，即使调用方重复取消，也等待线程结束并接管返回值，再清理租约/目录并传播取消；不取消线程的 asyncio 包装任务或在线程仍写入时删除快照。

同版本/配置 fingerprint 及同主模型对象/名称复用实例。配置或模型变化也受 fresh-turn 检查，包代码更新应递增 manifest.version。

## 事务与关闭

统一事务锁协调多个独立插件槽位。同批运行插件初始化失败时，清理候选并按保留快照恢复已改变绑定；不承诺普通插件任意 Python 副作用或业务文件的全局事务。

资源可实现 async aclose(*, timeout, grace_timeout)，负责停止自身调度、等待自身任务、清理专属附件。宿主先异步关闭，再 unload_extension；无完整 record 的部分初始化逐资源 unregister。默认总等待预算 10+5 秒，保留超时 closer 引用，不取消 awaiter 冒充线程退出；重复等待复用仍在运行的 closer，只有结束且返回 TimeoutError 的 closer 可重试。

超时保留 closing 绑定、依赖及看护，阻止新装备事务。失败的选择变更在收尾后恢复旧绑定，会话关闭则完成释放。未确认结束的异常不强行释放依赖。资源停止后才释放租约和包快照；仅移除本管理器新增的观测 Rail，采集是否关闭仍结合其他需求。

外部 unload_extension/unregister_rail 失败时，看护保留绑定和依赖并重试注销；不能仅凭 closer 已结束就释放仍挂载 Rail 的依赖。成功注销后再释放或恢复，持续失败时关闭操作仍受总等待预算约束并报告 closing_timeout。

新增插件在现有服务和生命周期契约内只需 ZIP；新宿主基础服务或新作用域仍需扩展宿主。SDK/管理器不得包含业务插件 ID、经验库 schema、专属选项或算法。

当前 test_ttse_plugin_runtime.py 保留预检、租约、作用域拒绝与普通零参插件验证。依赖新版 core 文件工厂的完整装卸、TTSE/notes/audit 并存与回滚用例已暂时删除；官方锁定依赖补齐能力后需恢复，记录见 B_20261031_1048_ttse-core-dependent-ut。
