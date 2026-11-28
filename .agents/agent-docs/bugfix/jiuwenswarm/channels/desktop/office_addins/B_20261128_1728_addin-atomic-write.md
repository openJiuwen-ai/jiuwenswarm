---
id: B_20261128_1728_addin-atomic-write
title: Office 加载项文件写入并发与占用失败
type: B
author: openjiuwen_ai
author_email: openjiuwen_ai@163.com
modules: [jiuwenswarm/channels/desktop/office_addins]
files:
  - jiuwenswarm/channels/desktop/office_addins/files.py
  - tests/unit_tests/test_office_addins.py
designs: []
design_note: 局部修复以仓库既有的唯一临时文件 + os.replace 原子写约定为依据，无独立设计。
---

# Office 加载项文件写入并发与占用失败

## 现象与根因

Issue #7369 中，Windows 上安装或重装 Office 加载项时，清单、CA 证书与 user-id 写入偶发失败，报 `PermissionError: [WinError 5]`；并发安装还会出现 `FileNotFoundError`，并可能残留指向旧内容的清单。

根因在 `write_atomic`：临时文件名固定为 `.<name>.tmp`，`os.replace` 无重试，失败后也不清理。两个写者复用同一个临时文件，一方 replace 成功后另一方再 replace 会抛 `FileNotFoundError`；Office 正持有清单句柄时，`os.replace` 直接抛 `PermissionError` 并留下 `.tmp`。

## 修复与决策

- 临时文件名改为 `.<name>.<uuid>.tmp`，并发写者各自持有独立文件，互不搬走或删除。
- `os.replace` 增加有限重试，最多 10 次；第 n 次失败后等待 `0.05 × n` 秒，累计约 2.25 秒后仍失败则抛出。Office 打开清单的时间窗口远小于该上限，指数级更长的等待没有必要，避免安装流程被长时间阻塞。
- 清理使用 `except BaseException`：`KeyboardInterrupt` 与 `SystemExit` 中断写入时同样删除本次临时文件，避免中断在磁盘上留下孤儿文件；其余异常照常向上抛出。

## 回归证据

在 Windows、Python 3.11 上运行 `tests/unit_tests/test_office_addins.py`，32 项通过、1 项跳过。新增 4 个用例：替换成功后无 `.tmp` 残留、读取方持有清单时重试成功、始终失败时保留旧文件并清理临时文件、20 线程 × 20 次并发写入内容完整。将 `write_atomic` 还原为修复前实现后，并发用例稳定出现线程异常、失败清理用例稳定抛错，修复后通过。真实 Office 宿主的联调未覆盖。
