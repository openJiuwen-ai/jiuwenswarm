# beta3 动态专家轻量配对

本改动基于 JiuwenSwarm `xiaoyi_0.2.4.beta3`，配对基于
`br_0.1.16.post2.hotfix` 的 Core。Core 只增加 `ExpertCandidate`、
`OrchestrationPlan`、`plan_experts` 及其内部规划辅助逻辑。Swarm 保留 beta3
原技能 Symphony 的 score 配置、构建、规划、RPC、工具和 Rail 实现。

## 依赖与合入顺序

当前 `pyproject.toml` 三处直接依赖与 `uv.lock` 锁定：

```text
git+ssh://git@gitcode.com/openJiuwen/agent-core.git@618a5fb0f28265c4cdaca3d3e3cb86f667c651b8
```

Core [PR #3298](https://gitcode.com/openJiuwen/agent-core/pull/3298) 已于
2026-10-20 合入官方 `br_0.1.16.post2.hotfix`，合并提交为上述 SHA。
该合并保留原提交历史，专家规划源码与此前 fork 的 `aaa582eaf` 一致；
上游另外包含日志日内大小上限调整。依赖声明与锁文件已切换到官方仓库，
其余包源与依赖版本保持不变。

下一步将 `lin-xiaoyu/jiuwenswarm:feat/beta3-symphony-merge-ready` 合入
`openJiuwen/jiuwenswarm:xiaoyi_0.2.4.beta3`。本分支以追加提交撤回了早期
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

定向验证使用脚本化模型输出和真实 Core 的工具、回调、中断及资源生命周期；
不代表真实模型触发、桌面联调或 Windows 安装包已验收。
