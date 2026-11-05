# sync_agents_configs 将MODEL_MODALITIES 视为可选

## 元信息

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_01 / sync-model-modalities-optional |
| 关联模块 | jiuwenswarm/server/runtime/sync_agents_configs.py |
| 最近一次修订日期 | 2026-11-05 |
| 关联 feature | 暂无 |

## 复现场景

存量智能体（如 `agentteam`）从未在前端保存过 `MODEL_MODALITIES`。前端每次 `sync_agents_configs`
都下发完整的 env 快照，但不会为存量配置补发新键，服务端随即返回
`agent '<id>': env missing required keys: MODEL_MODALITIES`，整单同步失败。

## 根因

`2e3386d1a` 把 `MODEL_MODALITIES` 加进 `SYNC_ENV_SCHEMA`。该集合同时承担"已知键"与
"必填键"两个语义，`_validate_env_schema` 直接用 `SYNC_ENV_SCHEMA - present` 求缺键，
于是新增能力键变成了硬必填。

而客户端契约允许不传：`config.yaml` 的 `modalities: ${MODEL_MODALITIES:-text}` 缺省回落
`text`，[[S_22]] 也把"未声明 `modalities`"定义为纯文本（解析 False）的正常分支。

## 修复口径

新增 `SYNC_ENV_OPTIONAL_KEYS`（当前仅 `MODEL_MODALITIES`），缺键判定改为
`SYNC_ENV_SCHEMA - SYNC_ENV_OPTIONAL_KEYS - present`。不做默认值注入：

- 缺键时沿用 `config.yaml` 的 `:-text` 回落，与 [[S_22]] 单一真源一致；
- 服务端不改写前端 env 快照，保留"不传"与"显式传 `text`"在语义上的区分。

其余键仍为必填，缺键照旧整单拒绝。

## 验证

`tests/unit_tests/agentserver/test_sync_agents_configs.py::TestSyncAgentsConfigsValidation`
新增 `test_model_modalities_optional_for_legacy_agents`（缺该键可通过校验）；原有
"缺其他必填键仍拒绝"用例保持不变。
