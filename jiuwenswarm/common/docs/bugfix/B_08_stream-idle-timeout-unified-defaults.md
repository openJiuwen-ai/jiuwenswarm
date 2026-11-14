# B_08 — 团队装配漏补流超时默认值；首包 300s 与看门狗撞车进不了重试

## 元信息

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_08 / stream-idle-timeout-unified-defaults |
| 关联模块 | `jiuwenswarm/common/stream_timeout_defaults.py`（新增）、`jiuwenswarm/resources/config.yaml`、`jiuwenswarm/server/runtime/agent_adapter/deep/model_factory.py`、`jiuwenswarm/agents/harness/team/config_loader.py`、`jiuwenswarm/agents/harness/team/team_manager.py`、`jiuwenswarm/agents/harness/common/rails/model_routing/model_routing_rail.py` |
| 修复日期 | 2026-11-14 |
| 关联 spec | 无（本次不改 S_10 契约叙述；只补宿主侧装配默认值） |
| 关联测试 | `tests/unit_tests/common/test_stream_timeout_defaults.py`、`tests/unit_tests/agentserver/test_model_cache_reload_retention.py`、`tests/unit_tests/agentserver/test_team_config_loader.py` |
| 测试基线 | 见本分支单测 |

## 现象与影响

1. **中途静默**：团队会话 `idle_timeout=60.0`，同进程单 Agent 为 `180.0`；长 tool 参数静默易提前流超时。
2. **OA.05000030（零首包）**：glm-5.3 建连后 300s `chunks=0`，relay 300s stall 看门狗 `chat.interrupt` → `CancelledError`，前端「团队回合长时间未响应」。`CancelledError` 不进 `LLMRetryRail`。

## 根因与修复决策

- 流超时默认值只挂在单 Agent factory；团队 ConfigLoader / model_pool / 路由兜底直构绕过。
- SDK 缺字段时 `stream_first_chunk_timeout=300` 与看门狗 300s 同刻竞争，interrupt 抢跑。
- **修复**（仅 jiuwenswarm，不改 relay 看门狗）：
  - `apply_stream_timeout_defaults`：键缺失 **或值为 `None`** 时补 `idle=180`、`first_chunk=180`（避免 YAML/relay null 被 setdefault 放过）
  - 四处装配共用；`config.yaml` 模板 `stream_first_chunk_timeout: 300→180`
  - `_build_agent_spec_dict` 对 **inline** `model.model_client_config`（无 `ref`）再套一次 helper，避免该形态绕过
  - `model_factory` 不再再导出私有 `_DEFAULT_STREAM_IDLE_*` 别名（常量只在 common）
- **为何 180**：明显小于看门狗 300s，无首 token 时先抛 `LLM stream timeout` 走重试；不改 watchdog，范围最小。
- **拒绝**：改 relay `OFFICE_CLAW_TEAM_STUCK_WATCHDOG_MS`；原地写回 `models.defaults`；本单不扩 evolution 旁路（P3 另跟）。

## 回归证据与适用范围

- 单测：缺字段补齐、显式保留、源 dict 不污染；团队 dict / 路由 builder / model_pool。
- 适用：relay 缺字段条目经四处装配路径。
- 未改：relay 看门狗默认 300s；旁路 LLM（evolution 等）二期按需接入。

## 防错模式

新建 `Model` / 打包 MCC 须经 `apply_stream_timeout_defaults`；`first_chunk` 须小于 team stall 看门狗，否则零首包进不了重试。

## 遗留

旁路 `ModelClientConfig(**…)` 按需二期接入同一 helper。
