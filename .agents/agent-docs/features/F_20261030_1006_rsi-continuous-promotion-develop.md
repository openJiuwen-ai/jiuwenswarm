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
  - pyproject.toml
  - uv.lock
  - tests/unit_tests/rsi/test_judge_profile_selection.py
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

## 2026-10-30 初次移植的遗留与生效条件

初次移植时，本分支仅准备双合候选，当时不能认定连续晋级可用。目标分支须先独立升级到支持 `continuous_two_stage_v2` 的 agent-core，然后重新验证配置加载、实际候选晋级、历史任务恢复和相关测试。本次不自动升级依赖，也不将这一依赖缺口改写成已通过。


## 2026-10-31 PR9297 UT 与依赖兼容修复

用户随后授权升级依赖并验证兼容性，本节替代上文“保持依赖不动、等待上游支持”的当前限制，保留初次验证事实。
CI 报告共 18901 passed、100 skipped、5 failed；本地原依赖复现为 materialization 5 failed、21 passed，
根因是 e08919883 没有 promotion / score_mode，未知配置被静默忽略。
直接使用 release e140e79cd 虽可消除五项失败，但缺少 develop 使用的 read_agent_template_card 等接口，不能采用。

配套 core 分支以 e08919883 为基线，移植已发布的运行时证据与连续两阶段晋级能力，保留模板读取和 develop API。
Swarm 的 pyproject.toml 三处 source 及 uv.lock 统一固定到
`https://atomgit.com/l_x_d/agent-core_mr.git` 的已提交版本 `94e389f3d3ce2e8eca3da571f46b84372af9b484`；
这是真实可获取的远程依赖，不再依赖本地 editable 文件。core 修复合入官方 develop 后，应切回官方仓库中的对应提交。
模型裁判测试改为验证连续分数保留 0.799 / 0.8，passed 仍由 0.8 阈值决定，不再断言 score 被二值化。

兼容性测试：core RSI / DeepAgent / ReAct 定向 2434 passed、3 skipped，日志隔离及跨 Task 推进回归最终 96 passed。
Swarm editable 联调 RSI 整组 285 passed；最终远程 pin 的普通安装来源已核对为 94e389f3d，RSI、依赖来源检查和 reload 定向复验共 329 passed。
Swarm 本地完整回归经安装前端依赖、改用短临时目录及普通 wheel 后为 18930 passed、75 skipped、1 failed、1 error。
失败的 Worker 退出时序用例在原始 PR d080ac46a + 原始 core e08919883 中同样以约 30 秒退出超过 5 秒断言失败，
属于既有本机基线问题，本修复不改动或跳过该用例；teardown error 来自 Zen tokenizer 网络预热等待，单独离线复验通过（包含在上述 329 项中）。
未执行真实模型端到端测试、静态检查或格式化；不将本地基线失败或待重跑的 CI 宣称为完整通过。


## 2026-11-02 合并 develop 的冲突处理

PR9297 源分支为 `fix/rsi-continuous-promotion-develop`，合并前为 `6892c0ce9`；
本次合并目标为 upstream/develop `8827ad7b5`，不操作 `0.2.8-6` 分支。
冲突仅涉及 pyproject.toml、uv.lock 和两份生成索引。保留目标分支新增的依赖、协议版本和文档，
保留本 PR 的连续晋级配置及测试，锁文件在目标分支锁定结果上重新解析，索引从合并后的文档生成。

目标分支将 core 升级到 c8c621b2，新增的 Symphony 接口不能被旧配套 core 覆盖。
因此先在既有 core 配套分支合并 c8c621b2，发布 `8a2dfe6e9a93b09a026cb07887675e8483f12455`，
再将 Swarm 三处 core source 一致固定到该远程提交；该提交同时包含 c8c621b2 的接口和连续晋级能力。
core 定向测试为 3129 passed、3 skipped，客户端工具及中断交互测试另有 119 passed。
Swarm 合并后的 RSI、Symphony、依赖来源及 adapter 定向验证共 524 passed，uv lock --check 和 uv sync --check 均通过。
本轮不重复执行完整 UT，不将此前完整回归结果当成本候选通过证据。
