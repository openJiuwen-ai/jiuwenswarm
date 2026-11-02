---
id: F_20261102_1011_gateway-memory-task-memory
title: 4.6 解决 memory 和 task memory 的 Gateway 解耦问题
type: F
author: ruiqiang2
author_email: renruiqiang@huawei.com
modules:
  - gateway/enterprise/config/manager_config
  - gateway/storage_assembly
  - jiuwenswarm/enterprise/config
files:
  - gateway/enterprise/config/manager_config/core/application_config/memory_config.py
  - gateway/enterprise/config/manager_config/core/application_config/task_memory_config.py
  - gateway/storage_assembly/setup.py
  - jiuwenswarm/enterprise/config/config_provider.py
designs: []
design_note: 用户于 2026-11-02 明确授权 4.6 memory/task memory 解耦，沿用既有通知和配置读取能力，无独立设计文档。
---

# 4.6 解决 memory 和 task memory 的 Gateway 解耦问题

## 依据与范围

基线 develop `41c1e95eb`；工作分支 `gatewa-dep-4.6-memoryandtaskmemory`。本任务编号与名称按用户本次指示使用。契约见 [配置归属与更新](../specs/jiuwenswarm/enterprise/config/S_01_memory-config-ownership.md)。

这是 memory/task memory 配置更新和关联冷加载的局部拆分；不修改有效配置 reader 的实现归属，不重构记忆系统。允许 jiuwenswarm 调用 Gateway，母仓别名和转导不列清理目标。

## 实际实现与取舍

两类 Service 删除对执行侧 apply 函数的直接调用；保存、字段更新、删除及返回值保持既有行为。原 application_config 路由已有写库后通知 Runtime 的机制，继续使用，不新增协议或通知链路。

同时删除 Gateway 两个存储装配入口的 `_cold_load_memory_configs()`，避免启动时再导入 Agent 实现。默认执行配置 provider 在企业配置解析前读取 memory/task memory，显式 refresh 复用同一读数过程。这样新进程和未收到 Runtime 通知的执行端，在下一次解析配置时也可取得当前持久化配置。

选择每次企业配置解析增加两类读库，避免另造缓存 TTL、轮询器或长生命周期管理器；数据库读取故障保留上次值。代价是额外两次表读取，未进行负载性能验收。不把已有 DB reader import 当成反向依赖问题，也不宣称其连接生命周期已经完成解耦。

## 验证记录（2026-11-02）

使用原仓库 `.venv` 的 Python 3.14.8，`PYTHONPATH` 指向本工作树，测试数据和日志写入临时目录。

- 单元回归：`tests/unit_tests/enterprise/config`、`tests/unit_tests/gateway/test_config_repository_order3.py`、`tests/unit_tests/agentserver/memory/test_config.py`、`tests/unit_tests/runtime/test_workspace_sqlite.py`。最终与下列进程集成测试合并执行，合计 **44 passed**。
- 独立进程集成：`tests/integration/test_memory_config_processes.py`，1 passed。使用真实 DbPersistentBackend 和 SQLite，Gateway 保存进程退出后，另一全新执行进程读取并断言 memory/task memory；删除后再次启动执行进程，断言回到 YAML 默认。
- 新增用例验证 Gateway 保存/删除不调用执行侧记忆实现；保存成功后才通知；写入失败不通知；memory revision 递增和 body 替换；DB 读失败保留缓存；YAML/社区版不新增 DB 读取。

上述测试均以 `python -m pytest ... -q --no-cov -o addopts=''` 执行。首次用例因测试存储桩缺少 list 接口失败，补齐桩后通过，不属于业务实现缺陷。

## 已知边界与交接

真实 agent-runtime `/config_refresh` Pod 日落链路、MySQL/PostgreSQL、多副本及并发乱序未运行；存量活跃对象不强制重建。自定义 provider 不因本次改动被强制改写。配置源与权限维度沿用既有部署，本次不补全全仓 storage 生命周期。

按 jw-review 对基线至工作区差异完成本地自查：核对 4 个业务文件、3 个测试文件、新增契约和中英文说明；未发现本次范围内新增阻断问题。这是本地自查，不表示独立评审、CI 或部署验收通过。

用户于 2026-11-02 确认配置 DB 归 Gateway 管理，允许 AgentServer 直接读取；随后授权将本次代码、测试及配套文档提交并推送到 fork 的同名分支。不创建 PR。
