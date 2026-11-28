---
id: B_20261126_1621_health-native-telemetry-exclusion
title: 健康探测排除 FastAPI 原生遥测
type: B
author: jrr
author_email: jirui635760263@qq.com
modules:
  - jiuwenswarm/server
files:
  - jiuwenswarm/server/agent_http_routes.py
  - tests/unit_tests/server/test_http_server.py
designs: []
design_note: 对既有健康接口的小范围修复，无独立设计文档；依据 Issue 7091 及已确认的精确路径排除方案。
---

# 健康探测排除 FastAPI 原生遥测

关联：[Issue #7091](https://gitcode.com/openJiuwen/jiuwenswarm/issues/7091)。
公仓基线：`8c5a154645575a46ec2fbb012dae6faf7fa633ab`。

## 现象与根因

FastAPI 0.165.0 在配置全局 OTel provider 后自动记录健康请求的 HTTP、
dependencies 和 endpoint span。周期性探针因此持续产生非业务 Trace，
Insight 可能将其识别为空任务。健康响应本身仍为 200。

## 修复决策与当前契约

在 `build_http_app()` 的 FastAPI 构造器接入 `telemetry.exclude`，精确匹配
HTTP GET `/api/v1/health`。查询参数不影响匹配；成功和失败探测均排除。
健康处理函数和普通应用日志继续执行，其他路径、方法及业务追踪继续采集。
原生排除同时跳过该请求的 Trace、指标与 OTel 日志，健康故障由探针状态和应用日志排查。

不关闭全局 OTel 或整个 HTTP tracing，以保留业务入口的上游 traceparent 关联。
不在导出后只丢弃根 span，避免残留孤立子 span。旧版 FastAPI 接受并忽略额外构造参数。

## 回归证据与适用范围

本次随代码提交真实 ASGITransport 和内存 span exporter 测试，覆盖健康响应、
重复请求、查询参数、失败探测、近似路径、其他方法及聊天 traceparent 关联。
测试隔离无关 Runtime 路由注册；聊天模型处理以生成业务 span 的测试处理函数代替。
Python 3.11、FastAPI 0.165.0、OTel SDK 1.45.2 下，公仓工作树的上述 8 项测试全部通过。
命令为：

```bash
python -m pytest tests/unit_tests/server/test_http_server.py -k health --confcutdir=tests/unit_tests/server -p tests.conftest --no-cov -p no:cacheprovider --timeout=20 --timeout-method=thread
```

该命令保留根 conftest 的 HOME/config 隔离及 caplog 传播恢复，但不加载
`tests/unit_tests/conftest.py` 的全局 Core checkpointer 夹具。标准命令在本地因缺少
`openjiuwen` 而出现 8 项 setup error，故不能将定向结果描述为标准全仓套件通过。
同一隔离环境运行完整 HTTP 文件得到 27 passed、17 failed；失败涉及缺少
`openjiuwen`、`a4p`、`requests`、`greenlet`，未验证标准完整回归通过。
另在独立 FastAPI 0.136.1 环境执行相同定向命令，得到 1 passed、7 skipped、36 deselected；
旧版没有原生遥测，相关 7 项按能力检测跳过，应用构造和健康响应验证通过。

## 资料与遗留

中英文 DebugTrace 文档同步说明排除范围和旧版兼容性。独立启用的 contrib
instrumentation 仍需自己的 URL 排除配置。本补丁不修改依赖版本、镜像构建或探针频率，
不清理历史数据，不解决 Insight 对其他管理请求的任务识别；真实模型与 OCP 现场验收未覆盖。
