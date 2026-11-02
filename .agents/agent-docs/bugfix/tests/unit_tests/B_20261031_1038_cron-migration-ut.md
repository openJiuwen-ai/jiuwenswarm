---
id: B_20261031_1038_cron-migration-ut
title: cron 会话迁移的实时流测试与类清点兼容修复
type: B
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - tests/unit_tests/gateway
  - tests/unit_tests/personal_context
files:
  - tests/unit_tests/gateway/test_cron_live_bus.py
  - tests/unit_tests/personal_context/test_closed_class_inventory.py
designs: []
design_note: 对现有测试契约和解释器兼容性的小范围修复，无独立设计。
---

# cron 会话迁移 UT 修复

## 现象与根因

2026-10-31 的 unit_test_report.html 对候选 677a3188d 报告两个失败。
实时流测试在新增 session.get_metadata 校验后仍传入通用 AsyncMock 请求和空 channel，
请求头被当成 coroutine，尚未进入 SSE 回放就失败。本地修复前复现相同错误。
类清点测试在 Linux CPython 3.11.5 的 ast.parse 中报告 AST constructor recursion depth mismatch，
未到达十八个类的断言，与 CPython 已知问题一致：
https://github.com/python/cpython/issues/106905 。本地 3.11.15 修复前该用例通过。

## 修复决策与备选

- 实时流测试按既有路由测试约定提供成功的元数据替身，并断言查询方法和 session_id；
  请求仅将 is_disconnected 声明为异步，隔离 Redis，继续验证旧帧和新帧的 replay 标记。
  不放宽生产入口的元数据及权限校验，权限/非 cron 拒绝路径由已有路由用例验证。
- 类名清点只需要识别 class 声明，改用 tokenize，避免旧解释器 AST 递归状态问题。
  保留十八个类的精确断言，补充文件/目录入口以及字符串、注释、嵌套类、局部类验证。
  不跳过断言或捕获异常后直接通过；不为静态类名清点引入子进程。

## 验证与限制

Windows、项目 .venv、Python 3.11.15，修复前两个文件共 1 failed / 7 passed。
修复后执行：

```powershell
.venv/Scripts/python.exe -m pytest tests/unit_tests/gateway/test_cron_live_bus.py tests/unit_tests/personal_context/test_closed_class_inventory.py tests/unit_tests/gateway/test_app_web_handlers.py -k 'cron_live or class_inventory or replay or redis_backlog or late_subscriber' --no-cov -q
```

结果：12 passed，262 deselected。覆盖 SSE 回放标记、实时帧、Redis 回放替身、
类清点及元数据成功/非 cron/403 三条路由路径。未执行全量 UT，未在报告的 Linux 3.11.5 环境重跑。
生产代码及接口契约未修改；解释器、依赖或入口协议后续变化时需重验相关用例。
