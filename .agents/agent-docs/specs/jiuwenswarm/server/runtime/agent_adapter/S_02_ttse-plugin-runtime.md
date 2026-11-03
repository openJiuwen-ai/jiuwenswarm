---
id: S_02_ttse-plugin-runtime
title: TTSE Coding 插件运行契约
type: S
author: xinyang_bali
author_email: 835397263@qq.com
modules:
  - jiuwenswarm/resources/plugins/ttse
files:
  - jiuwenswarm/resources/plugins/ttse/rails/ttse_plugin_rail.py
  - jiuwenswarm/resources/plugins/ttse/manifest.json
---

# TTSE Coding 插件运行契约

TTSE 2.0.0 使用 [S_03 通用运行契约](S_03_native-plugin-runtime.md)，声明 metadata.plugin_runtime version 1，profile code/main，互斥能力 ttse。原 coding_runtime v1/v2 与 1.x ZIP 被替代，需配套升级宿主和 ZIP。

## 包内职责

prepare_plugin_context 负责选项校验、旧配置兼容、embedding 解析和 bank 结构检查；from_plugin_context 负责 TTSEConfig、LLMInvokePolicy 和 Rail 实例化，并调用 runtime.register_resource。宿主没有 TTSE 专用管理器或构建/配置校验 SDK。

requires 为 trajectory、main_model、agent_data_workspace、embedding_factory，direct_tools 为 ttse_consult。算法、存储和安全关闭继承 agent-core 的 TTSERail。支持主 code normal/plan 及兼容别名，不向 team、work 或子 agent 自动挂载。

配置优先级：包内 DEFAULT_OPTIONS → config.yaml.ttse → host_config.react.ttse 业务选项 → plugins.ttse。允许 evolve_enabled、inject_enabled、dream_enabled、consult_top_k（整数 1–40）、consult_retrieve_mode（hybrid/embed/bm25）；布尔及字符串 true/false 显式校验。embedding 仅消费宿主配置，不允许包内默认值携带凭据。

包固定 batch_size=1、consult_rrf_k=60、dream interval/min_hours/ttl_days=50/24/90。归纳策略为 attempt_timeout_secs=300、total_budget_secs=300、max_attempts=2。主模型从声明服务成对获取，不单独创建客户端。

## 所有权和数据

code TTSE 只有插件或无 owner，不使用 adapter._ttse_rail 内置槽位；旧 enabled 不创建 code Rail，取消插件也不恢复内置实例。work/agent 内置 TTSE 保留原开关和独立 work 库。

bank 固定在 agent_data_workspace/.ttse/code/bank.json，不写用户项目。读取损坏 JSON 或非法 facts/tips/retired 结构时预检失败，原文件不重置。卸载保留经验数据；同数据根 code 项目共享 code 库，不承诺项目隔离或跨进程写安全。

## 运行和收尾

SYSTEM 使用指引、HISTORY 分类目录、ttse_consult 按需返回正文的语义不变。inject=false 时工具不存在，通用 direct_tools 声明不会创建无效工具。

装卸事务、配置/模型边界、租约与失败回滚遵循 S_03；包继承 TTSERail.aclose 等待归纳、dream 及元数据 flush，并清理本 Rail 的 catalog。宿主不直接调用 TTSE clear_catalog，不读取其后台任务字段。

验证使用版本化 `tests/fixtures/ttse_plugin` 契约夹具，逐测试复制到 tmp_path，不依赖未纳入版本控制的生产插件资源或本机已安装包。当前保留 ZIP 打包/导入、宿主预检、租约与普通插件验证；依赖新版 core 文件工厂的 TTSE 挂载、模型/span、并存与关闭回滚用例已按用户要求暂时删除，待官方依赖具备配套能力后恢复，见 B_20261031_1048_ttse-core-dependent-ut。现有 UT 通过不代表官方锁定版本已支持完整 TTSE 插件运行链。夹具不替代外部生产插件制品验证。打包器的默认生产源不变，支持 source/--source 显式指定；缺失默认资源时明确报错，不静默使用测试夹具。
