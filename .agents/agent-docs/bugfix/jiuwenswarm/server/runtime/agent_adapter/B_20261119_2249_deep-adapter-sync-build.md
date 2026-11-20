---
id: B_20261119_2249_deep-adapter-sync-build
title: DeepAdapter 冷启动同步构建阻塞事件循环
type: B
author: yangzequ
author_email: yangzequ1@huawei.com
modules:
  - jiuwenswarm/server/runtime/agent_adapter
files:
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/server/supervisor/diagnostics.py
  - tests/unit_tests/agentserver/test_deep_adapter_rail_build_timing.py
  - tests/unit_tests/agentserver/test_ipc_backpressure.py
  - tests/unit_tests/agentserver/permissions/test_permission_cold_build.py
  - tests/unit_tests/agentserver/permissions/test_permission_output_process_cli.py
designs: []
design_note: 本次是对现有 DeepAdapter 创建链路的局部阻塞修复，无独立设计文档。
---

# DeepAdapter 冷启动同步构建阻塞事件循环

## 现象与根因

`JiuWenSwarmDeepAdapter._create_instance()` 是异步函数，但在原实现中直接执行了同步的配置读取、sys operation 注册、工具发现、`_build_agent_rails()`、`_build_subagents_with_general_purpose()` 和 `create_deep_agent()`。其中 `_instantiate_rails()` 会逐个执行同步 rail builder，子 agent 阶段会读取配置、扫描文件并组装 `SubAgentConfig`；创建完成后的 `DeepAgent.ensure_initialized()` 又会在异步函数内部直接调用同步的 `rail.init()`。这些调用会独占 AgentServer 的 asyncio event loop。

整改前在 `develop-latest` 当前代码上，通过真实的 `_instantiate_rails()` 生产函数注入一个 `time.sleep(0.25)` 的可控 rail builder。构建耗时约 267–279 ms，期间 ticker 没有获得新的调度机会，仅保留构建前的初始 tick，复现了同步阻塞。

## 修复决策

1. 删除单用途的 `sync_executor.py`。DeepAdapter 内只保留用于保护共享 agent/resource registry 的私有并发槽；具体同步操作直接复用已有 `run_history_io()` 的 `asyncio.to_thread()`、shield 和取消收尾语义，并按职责分别调用。
2. `_create_instance()` 保留原有的分步编排：配置加载、sys operation 注册、工具卡片发现、rails 构建、子 agent specification 组装、`create_deep_agent()` 和 cron 工具注册分别处理；各自的同步实现通过明确命名的异步边界运行，rails、子 agent 和 DeepAgent factory 之间持有同一个 registry guard，避免共享注册表被并发修改。
3. 对调用方取消采用“等待 worker 完成、先发布资源结果后再传播取消”的语义。线程中的同步 I/O 或同步 rail 构建无法被安全停止，提前返回会让后续清理或重建与仍在运行的构建并发；结果发布先于取消传播，确保 sys operation 和 DeepAgent 实例仍可被清理。
4. `DeepAgent.ensure_initialized()` 使用适配器独占的长生命周期 worker 事件循环运行，覆盖其内部同步 `rail.init()`；初始化线程完成前不会释放 registry guard。适配器销毁时，先在同一 worker loop 上注销遗留 rails、释放 task-scoped resources，再关闭事件循环，避免 rail/resource 绑定到已关闭的临时 loop。

## 子 agent 边界

本次修复覆盖 DeepAdapter 冷启动时的子 agent specification 构建和注入，以及 factory 创建根 DeepAgent 的同步部分。OpenJiuwen 的具体 `DeepAgent.create_subagent()` 是 TaskTool/SessionSpawnExecutor 运行期按需调用的同步 API，并不在 DeepAdapter 冷启动时创建具体子实例；它需要依赖侧提供异步创建接口或在其调用方建立同等线程边界，不能在本次 adapter 创建修复中伪造一个同步等待异步任务的包装。

## 验证

- 整改前生产 `_instantiate_rails()` 探针复现：约 267–279 ms 无 event-loop 调度。
- `tests/unit_tests/agentserver/test_deep_adapter_rail_build_timing.py`：覆盖事件循环心跳、worker 执行和取消收尾。
- rails/初始化线程边界：覆盖真实 `_instantiate_rails()` 慢 builder、`ensure_initialized()` 内同步 `rail.init()` 的 worker 执行、worker loop 生命周期和取消后的 sys operation 接管。
- cron 注册线程边界：19 passed，覆盖启动期 cron 工具注册的 worker 执行及既有注册行为。
- 创建链路定向回归：34 passed in 24.29s，断言全部通过；pytest 进程退出阶段受 Windows 下测试日志 handler 未释放、临时目录删除触发 `WinError 32` 影响。
- CI 门禁报告中的 314 个失败节点按文件分组复测：原有同步 builder 的位置参数兼容问题已消失；权限回归、MoA、快照和 Process CLI 相关节点均通过。
- Windows 下补充验证：Process CLI 权限链路 11 passed，Process CLI 回归 9 passed；绑定文件使用扩展路径，避免深层 worktree 下超过 `MAX_PATH` 被误报为 `FileNotFoundError`。
- 快照测试显式按 UTF-8 读取其自身写出的 JSON，避免依赖 Windows 默认代码页；`test_permission_cold_snapshot.py` 35 passed。
- rebase 到 `origin/develop` 的 `b460e34522` 后，CI 报告（提交 `17367a6792`）为 27157 passed、247 skipped、1 failed；唯一失败是 `test_ipc_backpressure.py` 依赖不同 Python/Linux 版本的 `faulthandler` 输出包含固定函数名。诊断文件新增稳定的 `PYTHON STACK TRACE` 段标记，UT 改为验证稳定段顺序。
- 对 rebase 后暴露的 persistent initialization loop 生命周期补齐测试所有权清理：cold fixture 使用 adapter 原生 cleanup，Process CLI fake manager 模拟生产 manager cleanup；`test_permission_admission_edges.py` 5 passed，Process CLI 相关场景重跑通过。
- 本次 CI 唯一失败用例在 Windows 本地重跑通过；修改后通过 `git diff --check`，未运行格式化和静态检查。
- 新增/修改 Python 文件通过 `py_compile`，`git diff --check` 通过。

未执行格式化和静态检查，遵循本仓库“禁止运行格式化与静态检查工具”的协作约束；未修改依赖锁文件。

## 遗留风险

worker 无法中断已经进入 native 同步 I/O 的任务；当前实现因此限制共享 registry 操作并在取消时等待收尾。适配器销毁前会清理初始化 worker loop 上的 rails/resources，已通过 Process CLI 的交互、取消和二次卡片链路验证；运行期具体 `create_subagent()` 仍有各自的同步边界，尚未由本次 adapter 创建修复统一处理。
