# beta3 cloud 动态专家轻量配对

本改动以 JiuwenSwarm `xiaoyi_0.2.4.beta3_cloud` 为合入目标，当前上游基线为
`e6db64fc97704bccfd7fdc840cd5542834b05ac2`，沿用该上游的
`br_0.1.16.post2.hotfix.security` Core 分支。动态专家的 Core 增量只增加 `ExpertCandidate`、
`OrchestrationPlan`、`plan_experts` 及其内部规划辅助逻辑。Swarm 保留 beta3
原技能 Symphony 的 score 配置、构建、规划、RPC、工具和 Rail 实现。

继续使用原发布分支 `feat/beta3-symphony-merge-ready` 和 PR #8437，
将本 PR 的功能提交重放到上述 cloud 基线。cloud 的安全、云连接、
定时任务和技能禁用状态改动完整保留；旧 beta3 上游提交不再作为本 PR
的新增提交。重放前的提交保留在本地备份分支，PR 以 cloud 的最终差异为准。

## 依赖与合入顺序

`pyproject.toml` 三处直接依赖沿用 cloud 上游的 HTTPS 地址和 hotfix 安全分支：

```text
git+https://gitcode.com/openJiuwen/agent-core.git@br_0.1.16.post2.hotfix.security
```

`uv.lock` 记录上述分支，并将本次解析结果固定为
`25a298d465a50e93cee1b1995b36926d419bc499`；使用 `uv sync --frozen`
时安装该提交，后续升级 Core 时需同步刷新锁文件。

Core [PR #3298](https://gitcode.com/openJiuwen/agent-core/pull/3298) 已于
2026-10-20 合入官方 `br_0.1.16.post2.hotfix`（`618a5fb0f`），随后通过
[PR #3310](https://gitcode.com/openJiuwen/agent-core/pull/3310) 合入安全分支，
合并提交为本次锁定的 `25a298d4`。两者的 `openjiuwen/symphony/` 源码一致。
cloud 原锁定的 `5951559a` 缺少专家规划接口，因此本次同步更新解析提交；
继续保留上游安全版本及其依赖约束。安装依赖无需配置 GitCode SSH 密钥；
默认、HarmonyOS 和 CLI agents 依赖使用同一分支和锁定结果。
同时移除锁文件中已不在 `pyproject.toml` 声明的 `h11>=0.16` 元数据，
`h11` 仍锁定为 `0.16.0`；除 Core 的解析提交外，所有包的版本及下载源均保持不变。

下一步将 `lin-xiaoyu/jiuwenswarm:feat/beta3-symphony-merge-ready` 合入
`openJiuwen/jiuwenswarm:xiaoyi_0.2.4.beta3_cloud`。本分支以追加提交撤回了早期
完整 Symphony 迁移，应审查并合入当前分支的最终差异，建议 Squash；
不要单独挑选历史中的完整迁移提交。桌面专用面板可在后端合入后按需选择。

## 配置

```yaml
symphony:
  enabled: false
  dynamic_experts:
    enabled: null
  paths:
    skills_root: ""
    score_dir: ""
```

`symphony.enabled` 仍控制原技能编排，默认关闭。动态专家使用独立的
`symphony.dynamic_experts.enabled`：缺失或 `null` 跟随代码默认开启，
显式 `false` 关闭；字符串、数字等非法配置关闭。修改后重启 AgentServer。
升级保留新路径显式值；旧 `agents.dynamic_experts.enabled` 仅迁移显式
`true`，旧模板的 `false` 不覆盖新默认值。

## 执行边界

桌面普通 agent 新轮次提供真实 `symphony_plan_experts` 工具，并引导主助手
仅在用户明确要求调用专家时规划。固定专家和专家团沿用原流程。候选来自本地
`experts` 与 `experts_cache`，有效本地包优先，缓存补位，排除专家团。
规划输入仅包含专家 ID、名称和职责；执行时仍挂载专家模板及技能。

计划通过真实 `task_tool` 执行，独立节点最多并发 3 个，依赖节点接收前置结果，
阻止越序和重复调用。HITL 回答恢复同一轮次，完成、失败和取消时回收专家
绑定、技能及所属运行时资源。`expert_assets` 保留选中包的来源与头像信息。
输出流提前关闭时，先中止所属 Core 执行任务，再清理动态专家，避免等待仍在
规划中的任务；输出流关闭失败或被取消时仍执行专家清理，HITL 挂起继续保留轮次。

此前的定向验证使用脚本化模型输出和真实 Core 的工具、回调、中断及资源生命周期；
本次 rebase 核对最终源码差异及依赖配对，未重新运行功能测试，
不代表当前 cloud 基线上的真实模型触发、桌面联调或 Windows 安装包已验收。
