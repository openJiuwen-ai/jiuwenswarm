---
id: F_20261030_1006_rsi-continuous-promotion-develop
title: Harness RSI 连续晋级配置的 develop 移植
type: F
author: l_x_d
author_email: linxiangda@huawei.com
modules:
  - jiuwenswarm/agents/harness/common/rsi
files:
  - jiuwenswarm/agents/harness/common/rsi/materializer.py
  - tests/unit_tests/rsi/test_harness_provider.py
  - tests/unit_tests/rsi/test_rsi_materialization.py
designs: []
design_note: 用户已确认的连续晋级配置切换，移植到 develop；无独立设计文档。
---

# Harness RSI 连续晋级配置的 develop 移植

## 范围与决策

- 基线为 upstream/develop 的 `88fbdd2a1`，移植 `0.2.8-6` 分支中 `5510109d5..da8864ead` 的五个配置、测试和中英文说明文件的净改动。
- 新任务配置显式设置 `continuous_two_stage_v2`、连续分数，以及提分门槛和非目标退分容忍值各 0.05；移除旧策略选择字段，历史任务仍使用其保存的配置。
- 用户要求不修改依赖，保留 develop 原有 `pyproject.toml` 和 `uv.lock`。本次未引入 PR #9088 的其他功能。
- 这是同一策略切换的分支移植；未查到 develop 中已有的相关实施记录，故新增本记录。运行时晋级算法仍由 agent-core 提供。

## 实际验证

- `uv sync --locked --quiet` 成功，安装的是 develop 锁定的 agent-core `e089198835426cd7681761b0cc278670cd83031f`；`uv sync --check` 确认环境与锁文件一致。
- 通过真实 `RsiTaskMaterializer.materialize_validation_profile` 生成配置并调用上游加载器进行 smoke 验证时，配置对象没有 `promotion` 属性，触发 `AttributeError`。因此当前依赖尚不支持该策略，不能将配置生成成功视为连续晋级已经生效。
- 用户选择跳过 UT；本 develop 候选未执行单元、集成或模型端到端测试。此前另一分支、新版 agent-core 下的 55 项通过结果不能作为本候选通过证据。
- 核对补丁无冲突，且依赖文件相对 develop 基线无差异。未运行格式化或静态检查。

## 遗留与生效条件

本分支仅准备双合候选，当前不能认定连续晋级可用。目标分支须先独立升级到支持 `continuous_two_stage_v2` 的 agent-core，然后重新验证配置加载、实际候选晋级、历史任务恢复和相关测试。本次不自动升级依赖，也不将这一依赖缺口改写成已通过。
