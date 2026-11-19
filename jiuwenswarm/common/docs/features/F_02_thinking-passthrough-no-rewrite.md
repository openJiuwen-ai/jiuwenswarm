# 思考强度透传（OfficeAce catalog）

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-16 |
| 范围 | `jiuwenswarm/common/reasoning_injector.py`；单测 `tests/unit_tests/common/test_reasoning_injector.py`；契约 `docs/specs/S_10_model-and-reasoning.md` |
| 测试基线 | `pytest tests/unit_tests/common/test_reasoning_injector.py` |
| Refs | [#5227](https://gitcode.com/openJiuwen/jiuwenswarm/issues/5227) |
| 关联 spec | [[S_10_model-and-reasoning]] N8 |

## 背景

OfficeAce 将用户所选思考档位写成厂商原生参数，经 `sync_agents_configs` 写入
`models.defaults[].model_config_obj`（例如 `extra_body.thinking` + `reasoning_effort`）。
jiuwenswarm 只负责原样送到厂商请求；档位表与校验不在本侧。

旧路径 `reasoning_level` 在命中 DeepSeek 官方 / 百炼白名单时会删除 `reasoning_effort`
并重写 `extra_body`。若与显式厂商字段并存，会破坏透传。本特性只做防改写。
窗口档位见 server [[F_03_context-window-effective-denominator]] / [[S_06]]（正交）。

## 决策

1. 在 `inject_reasoning_params` 入口检测显式厂商思考字段；命中则只剥离
   `reasoning_level`，不做 N7 注入（S_10 N8）。
2. 仅有 `reasoning_level`、无显式字段时，旧注入行为不变，保证 Web/TUI 默认能力。
3. 不新增 rail、枚举、能力矩阵；不改 `vendor_map`；不做请求级思考覆盖。

## 拒绝的方案

| 方案 | 为何不做 |
|---|---|
| 按请求 `chat.send` 覆盖 `model_config_obj` | OfficeAce 确认走 catalog 同步 |
| 删除整个 `reasoning_level` 注入 | 会破坏 jiuwenswarm 自带界面旧能力 |
| 新增 `ModelOptionsRail` 写思考参数 | 与「透传、不维护」冲突 |

## 验证

- 单测：OfficeAce 透传、并存防改写、`enable_thinking=False`、旧路径官方/百炼、MaaS 无注入、无思考字段不变。
- 待联调：改档后 catalog 同步须先于下一条依赖新思考参数的消息；厂商 400 经现有错误链路回前端。

## 已知遗留

- 厂商报错原样回传未新增专项单测，联调时确认。
- 窗口字段禁入 `model_config_obj` 见 [[S_10]] N9；生效写入不在本 feature。
