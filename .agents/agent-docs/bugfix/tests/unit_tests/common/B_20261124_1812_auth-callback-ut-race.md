---
id: B_20261124_1812_auth-callback-ut-race
title: 登录回调 UT 的后台轮询时序竞态
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - tests/unit_tests/common
files:
  - tests/unit_tests/common/test_auth_server_callback.py
designs: []
design_note: 修复既有异步登录测试的时序假设，保持认证接口和实现不变，无独立设计。
---

# 登录回调 UT 的后台轮询时序竞态

## 现象与证据范围

用户提供的 `4f2d69fc-0166-4e9a-8aa6-1ff01b69efa0.zip` 包含 Linux CI 的完整文本日志。
它测试 PR !10796 的 `62b5127e6` 合入 develop `3f2ae160b` 的预览，结果为
27,942 passed、247 skipped、27 subtests passed、1 failed。
唯一失败是 `test_claimed_code_completes_the_login` 第一次 `claim()` 返回了已完成的会话，而断言要求 `None`。
日志附带的 JUnit 文本被截断，结果及失败归因使用完整的 pytest 汇总和 traceback，不将截断 XML 当作完整报告。

## 根因与修复决策

测试在调用 `create_authorization_request()` 前同时排入一条 pending 和一条成功响应。
该接口会启动后台线程；后台可能先消费 pending，随后前台第一次 `claim()` 便能取到成功响应。
两种调用顺序均符合认证接口，但测试固定假设前台一定先取到 pending。

在候选 `62b5127e6` 和未修改的 CI develop 基线 `3f2ae160b` 上，控制后台先完成授权，均复现相同断言失败。
这项失败不由 Worker 进程身份修改引入。

模拟服务先只提供 pending，在测试已断言等待状态后再提供授权码。
保留等待状态、完成登录、请求字段与 token 交换字段等断言；新增参数覆盖先等待一次后台轮询的顺序。
本次只调整测试数据的释放顺序，不变更 Gateway 的认证逻辑、依赖版本或生产超时。

## 回归与适用范围

- Windows / Python 3.11.15 / psutil 7.2.2；Gateway 为 `dda0e957bd5ae501a326d3ea61a4d336c91ebfc7`。
- 回调测试整文件 17 passed；两种轮询顺序各运行 20 次，共 40 passed。
- 含此文件的 19 文件 Windows 相关回归为 629 passed、8 skipped、31 deselected。
  排除项为 8 个在候选和未修改 CI 基线上均复现的 Windows 平台失败，以及 23 个 slow 用例；不计为通过。
- 首次扩大回归在既有依赖元数据读取中超时，无完整 JUnit 结果；独立读取基线与候选得到相同依赖摘要后，
  保持原 45 秒用例超时重跑，得到上述结果。该次超时不计为通过，也不据此推断 Worker 身份读取失败。

原始日志、基线对照、重复运行和 JUnit 位于本地隔离目录
`D:/work_space/.codex-temp/harmony-process-identity-ut-20261124/`；运行产物不提交。
修改后的 Linux 全仓 CI 需由新提交重新执行，本地 Windows 回归不能替代该结果。

## 关联验证

Worker 的 Windows 实进程对照与 HarmonyOS spec 核验续写于
[进程身份修复记录](../../../jiuwenswarm/server/B_20261123_1757_harmony-process-identity.md)。
本记录只负责认证 UT 竞态，不改变进程身份规约。
