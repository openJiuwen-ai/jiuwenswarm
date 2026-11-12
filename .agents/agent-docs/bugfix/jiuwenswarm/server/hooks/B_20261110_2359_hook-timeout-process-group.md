---
id: B_20261110_2359_hook-timeout-process-group
title: command hook 超时后孙进程孤儿化
type: B
author: zxy12_zxy
author_email: zxy12_zxy@noreply.gitcode.com
modules:
  - jiuwenswarm/server/hooks
files:
  - jiuwenswarm/server/hooks/executor.py
  - tests/unit_tests/server/hooks/test_executor.py
designs: []
design_note: 局部超时清理缺陷修复，对齐 command_tools 既有进程组清理模式，无独立设计。
---

# command hook 超时后孙进程孤儿化

## 现象与影响

`HookExecutor._run_command_hook` 以 `bash -c <command>` 执行 command hook，超时后仅对直接子进程调用 `proc.kill()`。当 hook 命令在后台拉起孙进程（如 `bash -c 'server &'`）时，孙进程继承 hook 管道句柄继续运行成为孤儿：进程不退出、管道写端不释放。

同仓 `command_tools.py` 已用 `start_new_session` + `os.killpg` + 管道 drain 修复了同类问题，hook 执行器路径未覆盖。定位基线为 upstream develop `e4b52a6901`。

## 根因

`asyncio.create_subprocess_exec` 未设置 `start_new_session`，hook 子进程与服务进程同属一个进程组；超时清理只命中直接子进程（bash），对孙进程不可见。`await proc.wait()` 返回后孙进程仍持有 stdout/stderr 管道的写端。

## 修复决策

1. POSIX 上创建 hook 子进程时设置 `start_new_session=True`，hook 子进程成为进程组长，其 pid 即 hook 派生进程树的 pgid。
2. 超时清理改用新增的 `_kill_hook_process_group(proc, pgid)`：先 `os.killpg(pgid, SIGKILL)` 清理整个进程组，再对直接子进程兜底 `proc.kill()`；Windows（`os.name == "nt"`）行为不变，仍只杀直接子进程。
3. 超时清理的回收等待同样有界：`proc.wait()` 以 `_CLEANUP_WAIT_TIMEOUT`（5s）封顶，进程组清理未命中时超时路径本身不挂死。
4. 不引入新配置项；正常路径（communicate + 退出码语义）不变。

## 回归范围

新增 `TestCommandHookTimeoutProcessGroup`：

- POSIX 端到端：hook 后台拉起 `sleep 60` 孙进程并前台挂起，超时后断言孙进程随进程组一并清理（psutil 僵尸态视为已清理），并断言超时路径在时限内返回（外层 `wait_for` 兜底，清理挂死时用例判失败）；
- `_kill_hook_process_group` 有 pgid 时先对整组 SIGKILL 再兜底杀子进程；
- 无 pgid（Windows / 未开新会话）时退回只杀直接子进程，killpg 不被调用。

既有 `TestCommandHookTimeout` 超时语义测试保持通过。

## 防错模式

子进程超时清理必须以"进程组"为清理单元，而不是"直接子进程"；新增子进程执行路径时对照 `command_tools._run_command_sync` 的既有模式（start_new_session + killpg + drain）自查。
