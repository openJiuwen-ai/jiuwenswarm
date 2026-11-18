---
id: B_20261118_1607_issue6809-source-free-exception-logging
title: Issue 6809 异常日志脱敏同步源码读取修复
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/common
files:
  - jiuwenswarm/common/utils.py
  - jiuwenswarm/common/log_exception.py
  - tests/unit_tests/common/test_log_exception.py
  - tests/unit_tests/agentserver/test_exception_logging_worker.py
  - tests/unit_tests/agentserver/fixtures/diagnostic_worker.py
designs: []
design_note: 针对现有源头脱敏与 Worker 日志隔离契约的小范围缺陷修复，无独立设计。
---

# Issue 6809 异常日志脱敏同步源码读取修复

## 现象与证据边界

[Issue #6809](https://gitcode.com/openJiuwen/jiuwenswarm/issues/6809) 报告长稳用例出现 OOM / `runtime worker closed`。
附件指出 `install_source_record_masking()` 在创建日志记录时同步调用 `traceback.format_exception()`，
可能进入 `linecache` 和文件系统。Issue 未提供完整现场日志，不能据此确认全部 OOM 成因或现场文件访问缓慢的原因。

本次基线为 develop `78c1fac71b2a1e313c91a31a9c39b6cf8a37b8c7` 与
`releases/0.2.9` `3202398f9169a89b42e20c011d36a9a2d28ecd9e`。
两个版本均保留该同步路径；源头工厂和 `SensitiveDataFilter` 的禁止源码访问回归均能暴露问题。

## 根因与修复决策

调用顺序为业务线程记录异常 → LogRecord 工厂格式化并脱敏 → handler / 后台队列 → 日志输出。
后台 handler 只能隔离后续 sink IO，无法消除工厂已经进行的源码查询。
仅设置 `TracebackException(lookup_lines=False)` 也不足以保证无 IO：栈提取仍可能调用 `linecache.checkcache()`。

新增 `format_log_exception()`，仅遍历内存中的 traceback 与异常元数据，保留文件名、行号、函数名、
异常类型、消息、cause/context、异常组和备注；省略源码行及局部变量。
两处脱敏入口均使用该函数，再复用原有敏感信息脱敏和 UTF-8 字节限制。
异常文本已存在时也清空 `exc_info`，防止日志记录持有原始异常帧及其局部变量。

格式化限制为总计 32 个异常、深度 8、每个异常 64 个栈帧、每组 15 个成员、每个异常 8 条备注，
文本总量不超过 64 Ki 字符；超限使用显式省略标记。共享的异常组成员仍分别显示，活动路径上的循环被截断。
该改动不把异常/局部变量移入后台队列，不改变心跳或 Worker 重启阈值。

## 回归证据

环境：Windows、CPython 3.13.9；两个分支分别使用 `uv sync --frozen` 创建环境，
实际安装的 agent-core 均为锁定的 `45d53dd1919a09e3801ce7bca3798f776ac72ca1`。

- 基线：两个分支的源码查询/异常组/显式 traceback 回归各失败 4 项。
- 真实 Worker 负向对照：0.2.9 的原生 IPC 与 loopback IPC 各触发一次注入的 `linecache.checkcache()`，
  日志调用均耗时约 2.001 秒。这是故障注入机制复现，不是原现场时长。
- 修复后：两分支四个真实 Worker 场景均为零源码查询；日志调用约 0.18–0.27 毫秒，
  请求完成、异常仍脱敏、传输 pong 与业务 loop beat 均推进、零重启、进程正常退出。
  fixture 只替换 Agent Runtime 构造，不使用真实模型；Worker 入口、dispatcher 与 OS IPC 均为真实实现。
- 相关回归：develop 的 173 项、0.2.9 的 174 项全部通过，包含异常日志、后台日志、脱敏、日志过滤和 Worker liveness。
- develop 扩展执行 `test_common_regressions.py`：另外 51 项通过，1 项 Windows 路径分隔符断言失败。
  该项在未修改的 develop 基线也失败，未纳入本次修复；完整扩展结果为 224 passed / 1 failed。
- 两分支均构建 WHL 并独立安装到临时目录，确认实际从安装目录加载新模块及 utils；
  注入禁止源码查询后，第三方 logger 的异常消息与 traceback 脱敏验证通过。

可复跑入口：`tests/unit_tests/common/test_log_exception.py`、`test_background_logging.py`、
`test_log_sanitizer_fragments.py`、`test_runtime_log_filter.py`、`tests/unit_tests/test_utils.py`、
`tests/unit_tests/agentserver/test_worker_loop_liveness.py` 与 `test_exception_logging_worker.py`。
使用 `python -m pytest <上述文件> -o addopts='' -o asyncio_mode=auto`。

## 防错约束与资料同步

源头日志脱敏在业务线程运行，异常诊断不能读取源码、访问 `linecache` 或捕获局部变量。
该约束落实在函数注释、禁止源码访问回归、异常帧释放回归和真实 Worker 故障注入中；
中文与英文开发指南同步说明异常日志省略源码行的行为。

## 未验证范围

未在原报障机器重放完整长稳/OOM 场景，未进行 Linux/macOS 实机验证。
本次结论覆盖上述同步源码读取缺陷及 Windows 控制实验，不代表所有 `runtime worker closed` 原因已消除。
