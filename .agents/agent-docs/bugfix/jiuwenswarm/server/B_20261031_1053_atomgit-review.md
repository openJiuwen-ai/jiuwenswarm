---
id: B_20261031_1053_atomgit-review
title: PR 9317 AtomGit 取消、补丁兼容与注销恢复修复
type: B
author: xinyang_bali
author_email: 835397263@qq.com
designs: []
design_note: 根据机器人检视修复现有资源生命周期和可选补丁边界，不引入独立方案。
modules:
  - jiuwenswarm/server/runtime/agent_adapter
  - jiuwenswarm
files:
  - jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/openjiuwen_fs_rwlock_patch.py
  - tests/unit_tests/agentserver/test_ttse_plugin_runtime.py
  - tests/unit_tests/agentserver/test_ttse_background_drain.py
  - tests/unit_tests/test_openjiuwen_fs_rwlock_patch.py
---

# 意见核对与修复

[PR 9317](https://gitcode.com/openJiuwen/jiuwenswarm/pull/9317/discuss) 的 AtomGit 六条建议均已核对源码和依赖；此前 openJiuwen-bot 两条修复见 B_20261031_1042_pr9317-review。

- 197074156：首次取消后的无保护 await 会被再次取消，使线程获取的 lease 无人接管；复制线程也可能在目录被清理后继续写入。统一通过 shield 等待线程任务结束，记录多次取消，赋值后再传播取消；异常收尾的 lease.release 同样等到线程结束。新增 acquisition/copying 两阶段三次取消用例，检查真实租约计数归零与临时目录消失。
- 197074157：旧评论的受保护方法访问已由此前组合式补丁重构消除；当前仍应防止 read/write/close 缺失。安装前检查公开 file_lock/read/write/close，缺失或不可调用时跳过并返回 False，不设置已安装标记；替换失败回滚已改方法。新增四个缺失接口用例，证明无部分安装且随后可正常安装。
- 197074158：实际 core BackgroundTask 有 wait，但原生 asyncio Task/Future 无 wait；宿主排空适配两种形态，Task/Future 通过 shield 等待，超时才走原有显式取消逻辑。新增真实 BackgroundTask、Task、Future 完成等待及超时后仍未退出写者保留用例。不宣称取消 asyncio awaiter 代表底层线程退出。
- 197074159、197074160：logging 与 RuntimeSupervisor 缺少导入的旧 Front 链已在前一轮删除，245 行；实际 Supervisor/Worker 启动路径不变，不在不可达代码中补导入。
- 197074166：注销连续失败确实会让原看护提前退出，但“closer done 就释放”不安全，Rail 可能仍被注册。改为保留绑定、租约、快照并在看护中重试注销；成功注销后才释放或恢复。持续失败继续门控装备变更、关闭按预算报错。新增连续两次 unload 异常再恢复的用例，验证失败时资源仍保留、成功后清空登记且计数归零。

## 验证

候选为 `8f28e293aac56229ece1d7a2005f06f939b2a97f` 上含前两轮修复的工作区。Windows Python 3.13.12，PYTHONPATH 首项为从 Git 导出的官方锁定 core `e089198835426cd7681761b0cc278670cd83031f`，随后为本仓与 `.git/ttse-ut-verify-deps`。未改 .venv/core/依赖锁；本次新增的是宿主行为测试，不恢复用户要求删除的新版 core 工厂集成测试。

定向三个测试文件：36 passed，14.83 秒。完整相关回归 **109 passed，16.92 秒**，使用 `-o addopts='' -o log_cli=false -o asyncio_mode=auto -q -n 2 --tb=short --show-capture=no`，范围为：

- agentserver/test_ttse_plugin_runtime.py、test_ttse_background_drain.py、test_code_adapter_ttse_rail.py、test_front_phase1.py、test_worker_required_preparation.py、test_worker_runtime_ownership.py；
- test_openjiuwen_fs_rwlock_patch.py、test_app_agentserver.py；
- server/extensions/test_ttse_plugin_package.py；
- agentserver/test_runtime_worker_phase2.py::test_front_and_supervisor_import_do_not_load_runtime。

以上路径均位于 tests/unit_tests。日志保存在本地 `.git/pr9317-all-review-regression.log`，不纳入提交。未运行格式化或静态检查，未重启服务，未重跑全量 UT 或 Linux CI。

## 交接

用户本轮授权修改完成后直接回复两个 bot，按本轮要求先回复本地结果，不等待推送。已完整展示正文，经 GitCode CLI 在六条 AtomGit、两条 openJiuwen 检视讨论中逐条发布，并明确本地修改尚未提交/推送，未将 PR 代码称为已更新。回读 API 的 diff_comment.reply，核实正文和账号 xinyang_bali 一致，八条回复 ID 为 197247958、197247964、197247971、197247978、197247987、197247996、197248006、197248014。未修改讨论解决状态。

提交/推送仍待单独确认。依赖新版 core 的完整 TTSE 插件链覆盖缺口继续按 B_20261031_1048_ttse-core-dependent-ut 处理。
