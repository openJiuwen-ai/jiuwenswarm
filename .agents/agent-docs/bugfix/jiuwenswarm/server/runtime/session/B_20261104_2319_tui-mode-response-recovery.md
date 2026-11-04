---
id: B_20261104_2319_tui-mode-response-recovery
title: TUI 未知旧模式恢复与幂等响应归一化
type: B
author: Yanqing-Shi
author_email: toddsyq@gmail.com
modules:
  - jiuwenswarm/server/runtime/session
  - jiuwenswarm/runtime
  - jiuwenswarm/channels/tui/frontend
  - gateway
files:
  - jiuwenswarm/server/runtime/session/session_metadata.py
  - jiuwenswarm/server/runtime/session/session_history.py
  - jiuwenswarm/runtime/service.py
  - jiuwenswarm/channels/tui/frontend/src/core/modes.ts
  - jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/mode.ts
  - jiuwenswarm/channels/tui/frontend/tests/run-tests.mjs
  - gateway/app_gateway.py
  - tests/unit_tests/runtime/test_session_mode_switch.py
designs:
  - D-20261101-1139-tui-session-mode-switch
---

# TUI 未知旧模式恢复与幂等响应归一化

## 现象与根因

基于 `90cf5d0f6788795937466dcdf35bbfcf85c7546e` 的检视已复现两条旧数据路径：持久化 `mode="unknown"` 时，`mode.set` 冲突后 `mode.get` 仍成功返回未知字符串，TUI 持续拦截聊天；持久化 `mode="team"` 时，同目标切换的幂等响应返回原始别名，客户端直接写入状态后将其误判为非 Team。缺失或 null 同样无法通过查询恢复。`deprecate_mode(None)` 返回 None，并不抛异常。

补充回归还发现，元数据旧别名迁移会把数字类型 mode 转成默认 Agent；客户端别名表直接索引可读到 `toString`、`constructor` 等原型属性。二者都违反成功响应必须包含受支持模式的约束。

## 决策与拒绝的方案

沿用关联设计的显式切换入口，当前契约见 [S_01_tui-mode-switch](../../../../../specs/jiuwenswarm/server/runtime/session/S_01_tui-mode-switch.md)。查询对无法解析的 mode 返回 `MODE_UNKNOWN` 和 `/mode` 修复提示；用户显式选择合法目标时，在既有跨进程锁内重新校验当前 mode，仅未知值可跳过期望模式比较，身份、忙碌、生命周期和运行态所有权检查继续生效。写入目标及 revision，未知 `previous_mode` 用 null 表达。已变成合法模式的会话仍拒绝过期的期望值。

不采用根据 `work_mode` 猜测默认路由：它表示项目索引，不能确定 Agent/Team 身份。不只把查询改成错误响应，否则没有实际恢复通道。合法旧别名的同目标请求保持幂等，不额外写磁盘或事件，仅把成功响应归一化。TUI 验证响应后才更新模式和确认标记，拒绝对象原型属性名；非法响应保留原显示并标为未确认。

Gateway 的共享消息导入移至早期 dotenv 解析之后的现有导入区。该变更恢复文件的顺序约定，不宣称此前已复现配置失效。普通发送后的粘贴清理反馈不成立，本次不调整粘贴生命周期。

## 回归证据

验证范围为上述基线之上的本次未提交修改。新增用例覆盖缺失/null/空串/未知字符串/非法类型、显式修复与后续冲突、用户隔离、忙碌及其他进程持有运行态、旧别名幂等响应，以及客户端非法响应后重试恢复。

Windows、Python 3.14.7 下，使用本仓 `.venv`、候选工作树的 `PYTHONPATH` 以及隔离的数据/临时目录执行 `uv run --active --no-sync pytest`，以下六组定向测试合计 **272 passed**：`test_session_mode_switch.py`、`test_mode_cross_layer_protocol.py`、`test_session_metadata_write_options.py`、`test_team_dispatch_mode.py`、`test_runtime_service.py`、`test_request.py`。新增模式修复用例直接修改临时 metadata 文件，通过 Runtime 公共入口查询和切换，并独立检查落盘模式、revision、项目归属及诊断事件；已知值恢复后的陈旧请求仍返回 `MODE_CONFLICT`。

Node 26.13.0 下，仅将修改的 TypeScript 模块转译为测试 ES module，未运行类型检查或格式化；完整 `npm test` 通过，包括新增非法响应、旧别名和重试恢复用例，以及编辑器交接、会话注册、SwarmFlow、状态栏等已有套件。沙箱环境最初阻止 pytest 临时目录及 Node 子进程，使用相同隔离目录在正常权限环境重跑后取得上述结果。

以上为本次修改的新验证证据，不沿用第一阶段结果。未执行真实模型、服务端连接或交互终端截图验证。

同步到主工作区后，以 `44919104b` 及原有未提交修改为基础重新验证，同样六组 Python 定向测试为 **267 passed**，完整 `npm test` 亦通过；不将独立工作树的 272 个用例计作主工作区结果。主工作区旧 TUI 测试夹具补齐缺失的 `entries`、`disconnect` 及测试结束后的状态清理，避免与实际 AppState/Editor 契约不符或定时器阻止测试进程退出。主工作区源码已直接转译后测试，未复用独立工作树的构建产物。

## 资料同步与遗留

同步当前 spec、中英文 TUI 使用指南及原 feature 的修订链接。不同执行者的模型上下文交接继续属于第二阶段，本次只修复路由恢复和响应边界。

后续 PR 10243 / gateway PR 24 兼容候选保留上述模式恢复和测试夹具修复；旧 `gateway/app_gateway.py` 导入修改由独立 gateway 的协议兼容注册承接。`artifacts/mode-fix-verification/restore_and_apply.py` 仅用于本地搬运，不属于提交范围。该候选的新增验证及范围见 [F_20261103_0545_tui-session-mode-switch-phase1](../../../../../features/F_20261103_0545_tui-session-mode-switch-phase1.md) 的本地兼容章节，上述历史结果不替代新候选验证。
