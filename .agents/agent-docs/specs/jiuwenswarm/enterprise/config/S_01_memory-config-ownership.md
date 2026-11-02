---
id: S_01_memory-config-ownership
title: Memory 与 Task Memory 配置归属与更新契约
type: S
author: ruiqiang2
author_email: renruiqiang@huawei.com
modules:
  - gateway/enterprise/config/manager_config
  - gateway/storage_assembly
  - jiuwenswarm/enterprise/config
files:
  - gateway/enterprise/config/manager_config/core/application_config/memory_config.py
  - gateway/enterprise/config/manager_config/core/application_config/task_memory_config.py
  - gateway/enterprise/config/manager_config/routers/application_config_routers.py
  - gateway/storage_assembly/setup.py
  - jiuwenswarm/enterprise/config/config_provider.py
---

# Memory 与 Task Memory 配置归属与更新契约

## 职责与依赖

Gateway 的 MemoryConfigService / TaskMemoryConfigService 负责持久化、返回保存结果，不导入执行侧记忆实现、不修改执行侧缓存。Gateway 存储装配只接线 Repository，不冷加载执行缓存。

`/memory`、`/task-memory` 的 PUT/DELETE 路由在写入成功后沿用 `trigger_runtime_config_update()`；失败不通知。通知沿既有 Runtime `/api/session/config_refresh` 路径防抖发出；没有 Runtime 地址时跳过，通知失败记录日志。HTTP 成功表示已保存，不表示所有执行实例已经应用。

执行侧 `DefaultConfigProvider.load_agent_config()` 在企业配置源下先调用既有两类 reload 函数，再合并企业配置。每次配置解析增加两类配置的读库，不增加轮询器、后台同步任务或第二份配置服务。`refresh()` 同样刷新两类缓存，支持既有 Agent 配置重载。

配置 DB 由 Gateway 管理，允许 AgentServer 直接读取，不要求新增配置查询服务。允许执行侧复用 Gateway reader；其连接单例、装配与关闭责任属于独立生命周期核查范围，本次不通过复制 reader 或更换 import 声称解决这些问题。

## 应用语义

- Memory：保存整段 body 并递增 revision；执行时将 DB overlay 深合并到 YAML memory 段。替换和删除后的下一次企业配置解析读取当前 DB，删除回到 YAML。
- Task Memory：沿用现有字段更新规则及 DB 优先于 YAML 的缓存；删除后回到 YAML。
- 企业配置读取异常沿用现有行为：记录警告，保留最近已应用的缓存；不将异常解释为配置删除。不新增撤权时限保证。
- YAML 配置源、社区版不新增企业库读取。自定义 ConfigProvider 继续遵循既有 SPI，不强制采用默认实现的读取策略。
- 当前表仍遵循每 Gateway 独立 DB 的既有部署前提；本次不新增租户维度、事务、版本排序或共享存储拓扑。

Runtime 通知沿用老 Pod 日落、存量会话保留亲和的机制；不承诺立即重建全部活跃会话的记忆对象。正在运行且不再次解析配置的对象，不由本次读库入口强制修改。

## 验证入口

- `tests/unit_tests/enterprise/config/test_memory_config_decoupling.py`：真实 Service / Repository 与执行配置入口，模拟存储边界；更新、删除、读写失败及通知顺序。
- `tests/unit_tests/gateway/test_config_repository_order3.py`：两类 Gateway 装配入口不加载执行记忆缓存。
- `tests/integration/test_memory_config_processes.py`：真实 SQLite 后端、独立进程保存/读取/删除/再次读取。

SQLite 验证不代替真实 Runtime Pod 刷新、MySQL/PostgreSQL、多副本、并发乱序或存量活跃对象验收。
