---
id: B_20261112_1814_pool-free-member-models
title: 空共享模型池保留成员独立模型
type: B
author: LaiYongqiang
author_email: laiyongqiang1@huawei.com
modules:
  - jiuwenswarm/agents/harness/team
files:
  - jiuwenswarm/agents/harness/team/team_manager.py
  - tests/unit_tests/test_model_routing_placeholder.py
designs: []
design_note: Issue 6496 与 MR 10012 的回归修复，依据既有成员模型回退契约，无独立设计。
---

# 空共享模型池保留成员独立模型

## 现象与影响

MR !10012 的候选 fd6ca8ef21911bca0041c01c4d2e56f0e39c4657 在没有可用全局模型时直接抛出
`MODEL_SELECTION_DISABLED`。即使 leader 和 teammate 各自配置了有效模型，Team 也无法加载。
共享配置为空或只有模板占位条目时均可复现。

## 关联设计与当前契约

`config_loader._build_agent_spec_dict` 保留成员显式模型，只为缺少模型的成员填充全局默认。
Core 的 `build_model_allocator` 在无共享池时返回 None，成员从各自的 spec 解析模型。
空共享池不等于无可用成员模型；同时，不允许依赖缺失或占位默认模型的成员进入执行。
Core 契约以 MR 所锁定的 f3f8f576164cc405c046e43079c8ad00ee17f0c4 源码核对。

## 根因与修复决策

占位过滤使全局有效列表为空，新增的空池门禁却将共享池误当作唯一模型来源。
修复在空池分支逐一检查合并后的成员模型：请求模型名必须非空，连接和请求配置不得包含已知占位值。
全部成员满足条件时保留无池执行；任一成员缺失或使用占位模型时仍提前报错。
不直接删除门禁，以免恢复全占位配置的执行路径；不把成员模型写回共享池，避免改变成员分配语义。

## 回归证据与适用范围

- 修改前：通过真实 `_load_team_spec` 入口，成员独立模型的两个正例失败，缺失成员回退的六个负例通过。
- 修改后：模型占位、Team 配置、模型选择、配方准备、迁移、运行时目录与模型资源相关七个测试文件，
  合计 238 passed、2 skipped。新增用例覆盖空配置和占位全局配置、leader/teammate/both 缺失、
  显式占位成员、自动补出的 teammate；禁用占位路由同时验证编译后只有有效部署。
- 命令：激活已有 `.venv` 并设置 `PYTHONPATH=.` 后执行 `python -m pytest`，
  指定上述文件，使用 `-q -o addopts='' -p no:cacheprovider`，不生成覆盖率产物。
- 环境：Python 3.12，本地现有依赖；安装的 Core/Gateway 版本与 MR 锁定版本不同，
  本次未替换环境，锁定版本的无池契约通过源码核对。未运行真实模型调用或浏览器端到端验收。

## 防错模式与资料同步

共享资源可选时，应验证最终消费方的配置来源，不能由共享列表为空推断消费方不可执行。
本次以真实 Team 加载入口的正负回归固化约束，未复制 Core 设计或改写归档设计。

## 遗留与后续

真实服务的模型请求结果仍需在匹配锁定依赖的运行环境中验收；单元回归不代表线上 Issue 已端到端关闭。

## 2026-11-12 对齐上游后的复验

基线更新为 develop `4bf143963a4dafa2202620a825a76c6dedc6a310`。
`config_loader.py` 的新增辅助函数位置冲突，保留上游 `_entry_model_id` 和本修复的
`_prefer_explicit_default`；保留上游向成员默认与共享池传递稳定模型 ID 的调用链。
新增三个真实 `_load_team_spec` 回归：同名模型按各自 ID 选中对应地址和凭据，
共享池首项与选择一致且过滤占位；显式占位 ID 仍提前报错。
上述七个测试文件复验为 **241 passed、2 skipped**，环境及端到端边界同前。
