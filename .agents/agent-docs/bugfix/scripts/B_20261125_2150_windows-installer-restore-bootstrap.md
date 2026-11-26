---
id: B_20261125_2150_windows-installer-restore-bootstrap
title: Windows 安装器恢复进程隔离初始化与分派测试修正
type: B
author: XinchenZzz
author_email: 279219073@qq.com
modules:
  - scripts
  - jiuwenswarm/common
files:
  - scripts/jiuwenswarm_exe_entry.py
  - scripts/windows-cli-preservation.iss
  - jiuwenswarm/common/windows_upgrade_config.py
  - jiuwenswarm/common/utils.py
  - tests/unit_tests/extensions_v2/management/test_local_cli.py
  - tests/unit_tests/common/test_windows_cli_paths.py
designs: []
design_note: 对 Issue 7056 已有恢复事务的局部故障修复及测试适配，没有新增独立设计。
---

# Windows 安装器恢复进程修复记录

## 现象与影响

Issue #7056 的 Windows 升级恢复命令由 Inno Setup 的 `RunCliProcess` 启动。
PR !10964（releases/0.2.10）及 !10965（develop）的原始验证记录表明：同一路径直接读取成功，
经安装器启动后出现 WinError 448。恢复进程应只执行配置事务，而非完整应用初始化。
本次未重新执行安装器复现，不将原 PR 的 Windows 验证记为本次验证。

!10965 提交 `368a35bbdff1ce6212a500a63db41cf0ecfd3b30` 的 CI 报告另有两个失败：
`test_frozen_entry_dispatches_before_single_instance_lock[False/True]` 均报
`NameError: name 'RESTORE_CONFIG_FLAG' is not defined`，发生在测试抽取的函数内。

## 关联设计与当前契约

没有独立 D。安装器调用 `--desktop-restore-upgrade-config`，由既有配置锁、DPAPI 快照解密、
哈希和配置状态校验保护恢复事务。并发修改或校验失败时保留恢复材料，不覆盖用户的新配置。

## 根因与修复决策

- `is_windows_installer_restore()` 只识别 Windows、frozen 且首个参数为恢复标志的进程。
  `_dispatch()` 使用成员判断并保留标志；不可换回 `_pop_flag`，否则恢复函数延迟导入 utils 时，
  模块级判定看不到该标志，会重新执行应用初始化。
- 恢复模式跳过 frozen cwd 初始化块，包括 npm 环境初始化、PATH 前置、控制台编码设置和
  `JIUWENSWARM_EXECUTABLE` 设置。恢复事务使用绝对数据路径，不依赖这些应用运行准备。
- utils 跳过 `setup_logger()`，避免日志目录创建访问受限 junction。develop 同时跳过
  `migrate_profile_data_root_layout()`，避免恢复子进程执行目录搬迁和兼容链接创建；
  0.2.10 不应为对齐此说明而引入 develop 的目录迁移逻辑。
- 恢复异常由入口 `main()` 捕获、尝试 `_write_child_error()` 后以非零状态退出；
  写错误日志失败也不会阻止退出。安装器 `RestoreWindowsCliConfiguration()` 检查进程结果，
  经 `MsgBox` 提示失败及快照位置，恢复成功不依赖应用文件日志。
- UT 只用 AST 抽取 `_dispatch()`，绕过真实入口的常量导入。保持该隔离测试的方式，
  仅在 namespace 补入 `RESTORE_CONFIG_FLAG`，不为测试通过改变生产恢复逻辑。

## 回归证据与适用范围

原 CI 报告记录 Linux / Python 3.11.5、origin/develop、提交 `368a35bbdf`；
两个失败均为上述缺失常量，没有 SDK 导入失败。既有最小对照仅补 namespace 常量并替代 CLI handler 后，
frozen 两种取值均返回预期状态 3，且传入 `['query', '--json']`。

本次在 `368a35bbdf` 同步 develop `1efdeda99f` 后的候选上，使用 Windows / Python 3.11.15，
执行 `python -B -m pytest -o addopts='' -p no:cacheprovider
tests/unit_tests/extensions_v2/management/test_local_cli.py::test_frozen_entry_dispatches_before_single_instance_lock
tests/unit_tests/test_windows_uninstall_guard.py -q`，18 passed，包含原失败的两种参数组合及卸载入口回归。
测试使用当前任务源码和已有依赖环境，不等于重建锁定环境或验证完整 EXE。
既有冻结恢复探针在独立解释器中用 audit hook 拦截 npm 导入及
日志目录创建，并覆盖无快照和配置恢复路径；其完整 Windows 安装器行为仍需发布验收。

## 防错模式与资料同步

新增函数全局依赖时，同时核对 AST 抽取测试自建的 namespace。保留已有参数化用例及业务调用断言，
不额外复制同类测试。本记录集中说明 argv 保留、初始化隔离及安装器错误上报的配合关系，并同步文档索引。

## 遗留与后续

本次仅适配 develop 的 UT 并补文档，不更改 0.2.10 生产代码或向该分支补交 UT。
恢复模式判据统一及 AppMutex 排除属于可选重构，本次不改变既有行为；安装器恢复在后期运行，
不能仅凭辅助进程短暂持有广告互斥量就认定发生安装阻塞。
注释保持仓库要求的英文，不采纳仅因周边中文注释而改成中文的建议。
