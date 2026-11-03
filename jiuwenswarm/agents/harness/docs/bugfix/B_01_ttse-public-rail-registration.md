# 子 Agent TTSE 装配访问受保护成员的整改

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-03 |
| 范围 | `common/rails/subagent_ttse_consult_rail.py`、`common/tools/subagent_executor/ttse.py` |
| 关联 spec | [S_11 子代理创建与分发](../specs/S_11_subagent-creation-and-dispatch.md) |

## 现象与根因

Codecheck 报告 G.CLS.11 共 8 处：共享 eager 状态使用受保护属性，已初始化实例通过改写
SDK 的 `_ensure_initialized` 补挂 TTSE Rail，并读取 Rail 的内部 agent 引用判断是否已挂载。

## 修复决策

自有共享状态使用带命名空间的公开元数据，保持引用计数和原始 eager 状态的恢复语义。
通过公开 `is_initialized` 判断实例状态；未初始化实例沿用 `add_rail`。
已初始化实例在公开 `invoke` / `stream` 执行前，用同一把锁通过 `is_registered_rail` /
`register_rail` 补挂，不在创建阶段启动后台注册任务。流入口关闭时同步关闭原始流。
未采用反射读写受保护方法或忽略规则来绕过报告。

## 回归范围

`tests/unit_tests/agentserver/test_subagent_ttse_consult.py` 覆盖并发 invoke/stream 只注册一次、
重复调用、流提前关闭、多个子 Agent 共享 eager 引用，以及原工具已在 eager 名单中的保留行为。
使用 `.venv` 与本地 `D:/codes/agent-core-devstable`，在临时数据目录运行 TTSE 与
`test_subagent_runtime_wiring.py` 回归，最终候选 39 项通过（4.88 秒），另有一项依赖弃用警告。
未运行 Codecheck 平台，未验证真实模型请求；本次模型行为测试使用可控模型替身。

## PR 9476 检视后续

检视指出延迟注册普通异常会阻断子任务。公开执行入口现对普通异常记录 warning，并通过
`unregister_rail` 回滚部分注册后继续执行；取消信号同样清理，但继续上抛，不吞掉真实取消。
新增 invoke/stream 两入口的失败与取消回归，并验证后续执行可重试恢复。

移除 `TTSEConsultDependencies.retrieve_mode` 冗余字段：底层 index 已从共享 store 的实时配置
选择召回模式，测试通过真实 consult 入口验证 bm25/embed/hybrid 三种配置，不复制另一份模式状态。
同步 `uninit` 在无运行循环时用临时循环完成附件清理，通过真实 PromptAttachmentManager 验证。

回退 PR 中无关的 SkillTurbo 默认关闭与三条新增状态日志，保持上游默认行为。
最终候选运行 TTSE、子 Agent runtime wiring、SkillTurbo prompt 三组回归，48 项通过
（6.06 秒），一项依赖弃用警告。Codecheck 平台复扫及真实模型部署验证尚未执行。
