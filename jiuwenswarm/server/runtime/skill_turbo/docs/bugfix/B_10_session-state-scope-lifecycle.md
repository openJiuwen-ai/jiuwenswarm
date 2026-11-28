# 会话状态元数据生命周期

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 日期 | 2026-11-28 |
| 基线 | PR 11161，a7d893477 |
| 关联规约 | S_06、S_10 N2 |
| 状态 | 修复及回归完成 |

## 问题与根因

isolated_skill_state 按 (card_id, sid) 向三个进程级字典添加锁、调用方弱集合和已访问键，
会话对象回收后，字典的身份键、锁和空集合仍无回收路径。WeakSet 只弱持有调用方，
不能回收外层字典条目。历史会话增加会持续积累元数据。

## 修复边界

同一身份共用一个包含锁、调用方 WeakSet 和键集合的 scope。身份索引使用 WeakValueDictionary，
调用方索引使用 WeakKeyDictionary，并以 scope 为值；scope 不强持有调用方，避免反向保活。
活跃操作和等待锁的操作也持有 scope，保证并发入口不会为同身份创建不同的写锁。

存活调用方跨提交仍需接收最新状态，因此不按提交完成、业务终态或清理命名空间立即删除。
最后一个使用者释放后自动回收。只调整进程内管理信息，不修改持久化台账、产物桶、断点、
公开工具或 isolated_skill_state 调用接口。本轮不修改其他两条检视意见。

## 验证

使用锁文件指定的 agent-core 228bd7da2 源码、Python 3.13.14 及原 venv 的其他依赖，
真实 SDK Session 与独立 InMemoryCheckpointer；所有业务数据仅位于测试实例或临时目录。
修复前：1000 个不同会话完成只读访问后，弱引用确认所有调用方均已回收，但锁表仍有 1000 项，
新增回收断言失败（1 failed，1.55s）。修复后相同场景验证两个弱索引均为零。

定向回归：生命周期、台账及产物命名空间共 41 passed（4.26s）。随后补充活跃写入取消、
ACK/提交确认失败及跨事件循环重建场景，新增生命周期用例共 12 项。
完整模块及旧工具、审批/HITL/恢复回归：740 passed（21.22s）。命令如下：

```powershell
$env:PYTHONPATH = (Get-Location).Path + ';E:/Jiuwenswarm-zqh/agent-core-pr1-sdk-20261127'
& E:/Jiuwenswarm-zqh/jiuwenswarm/.venv/Scripts/python.exe -m pytest tests/unit_tests/server/runtime/skill_turbo/ tests/unit_tests/agentserver/test_skill_turbo_tools.py tests/unit_tests/agentserver/test_skill_turbo_prompt_disabled.py tests/unit_tests/agentserver/test_skill_turbo_interrupt_recovery.py tests/unit_tests/agentserver/test_skill_turbo_hitl_resume_react.py tests/unit_tests/agentserver/test_skill_turbo_hitl_tie_rewrite_repro.py tests/unit_tests/agentserver/test_skill_turbo_resume_usage.py -q -o addopts="--asyncio-mode=auto" -o log_cli=false --tb=short --show-capture=no
```

验证涵盖存活调用方跨提交保留同一 scope、同步更新与删除后的旧快照回写、回收后重建会话仍能
读取已存数据、排队与后来写入共享锁、等待者及持锁者取消、初始化/业务/提交/ACK/确认失败后
回收与重试。跨 loop 验证仅针对上一轮所有使用者已释放后的重建，不扩展到跨 loop 共用活会话。

日志位于本机临时目录 pr11161-state-lifecycle-20261128 下的 red.log、targeted.log、regression.log。
未运行格式化或静态检查，未进行独立代码检视或真实服务长时间压测。接口、持久化键与数据结构
保持原契约；后续 PR2 继承本修复，测试 fixture 使用新的弱索引，不恢复三个旧全局表。
