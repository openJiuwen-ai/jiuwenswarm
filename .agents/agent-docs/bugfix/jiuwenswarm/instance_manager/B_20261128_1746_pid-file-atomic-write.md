---
id: B_20261128_1746_pid-file-atomic-write
title: 实例 PID 文件写入非原子与占用失败
type: B
author: openjiuwen_ai
author_email: openjiuwen_ai@163.com
modules: [jiuwenswarm/instance_manager]
files:
  - jiuwenswarm/instance_manager/lock.py
  - tests/unit_tests/test_instance_manager.py
designs: []
design_note: 局部修复以仓库既有的唯一临时文件 + os.replace 原子写约定为依据，无独立设计。
---

# 实例 PID 文件写入非原子与占用失败

## 现象与根因

Issue #7371 中，Windows 上启动或重启实例时 `.instance.pid` 写入偶发失败，抛出 `PermissionError: [WinError 32]`（文件被其他进程或索引器占用），失败后目录残留 `.instance.tmp`；并发启动还会互相删除临时文件；`status` 可能在替换窗口内误报「实例未运行」。

根因在 `write_pid_file`：临时文件名固定为 `.instance.tmp`，两个写者会互相覆盖或搬走；写入路径为 `pid_path.unlink()` 后 `temp_path.rename(pid_path)`，unlink 与 rename 之间存在目标文件缺失窗口，违反原子替换语义；`rename` 没有重试，占用时直接失败且不清理临时文件。持有 PID 文件句柄时调用 `write_pid_file` 可 100% 复现 `PermissionError(winerror=32)`。

## 修复与决策

- 临时文件名改为 `.<name>.<uuid>.tmp`，并发写者持有独立文件，最后完成的 `os.replace` 生效，语义与旧实现「最后写者胜出」一致，且任意时刻读取方都能看到完整旧文件或完整新文件。
- 用 `os.replace` 一步替换，去掉先 `unlink` 的步骤，消除目标缺失窗口。
- 重试不按键控 errno/winerror 判定：POSIX 与 Windows 的 `os.replace` 在文件被占用时都映射为 `PermissionError`，因此对 `PermissionError` 无条件重试；最多 10 次，第 n 次失败后等待 `0.05 × n` 秒，累计约 2.25 秒上限。PID 写入在实例启动时只发生一次，该上限足以覆盖句柄释放窗口，又不会长时间阻塞启动流程。
- 清理使用 `except BaseException`：`KeyboardInterrupt` 与 `SystemExit` 中断时同样删除本次临时文件后重新抛出，避免中断留下孤儿文件。
- 未处理 `SIGKILL` 造成的临时文件残留（进程被强杀时任何清理逻辑都不会执行）。该窗口为毫秒级且写入只在启动时发生，残留文件名唯一、不会破坏后续写入，暂不引入启动期扫描，避免误删其他写者正在使用的临时文件。

## 回归证据

在 Windows、Python 3.11 上运行 `tests/unit_tests/test_instance_manager.py`：118 项通过；`TestIssue2788SafeStop` 的 8 项失败是 Windows 无 `os.getpgid` 的历史平台限制，与本次改动无关（Linux CI 正常）。新增 3 个用例：重复写入后无 `.tmp` 残留、读取方持有目标文件时第 3 次重试成功、始终失败时保留旧 PID 且清理临时文件并重试满 10 次。将 `lock.py` 还原为修复前实现后，后两个用例稳定失败，修复后通过。真实多进程启动联调未覆盖。
