# Petal 搜索在请求中无法读取热下发后的最新环境配置

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-28 |
| 严重程度 | 中（影响请求执行中的 Petal 搜索配置热更新） |
| 范围 | `local_env_config.py`、`agents/harness/common/tools/web_search/paid.py` |
| 测试基线 | `51 passed`（common/local_env_config + agents/harness web_search） |
| 关联 spec | `S_08_config-and-environment.md` |

## 现象

`agent.reload_config` 热下发 `PETAL_SEARCH_HEADERS` 后，请求仍在执行时，Petal 搜索
发起 HTTP 请求仍读取请求开始时的旧 headers；同类配置 `default_headers` 在 reload
路径中可见新值。

## 根因

`PETAL_SEARCH_HEADERS` 原先通过 `read_env()` 读取。overlay 绑定后，`read_env()`
遵循 sealed 语义：只读任务级 overlay 快照，miss 即 unset，不回退 live tip。请求
执行中的 reload 只更新 `effective_tip()`（active ∪ staged，staged 胜），不会修改已
绑定的 overlay，因此 `petal_search_sync()` 在工具调用阶段读到旧值。

## 决策

新增 `read_env_live()` 作为 [[S_08]] N6 的显式例外，供允许观测请求中 reload 的
消费者使用；当前生产消费点仅限 Petal 搜索的：

- `PETAL_SEARCH_URL`
- `PETAL_SEARCH_HEADERS`

该入口直接读取当前 namespace 的 `effective_tip()`，绕过任务 sealed overlay。若
overlay 已绑定且 live tip miss，仍按 unset 返回 default，不回退 sealed overlay 或
进程环境变量。普通 `get_local_config` / `read_env[_if_set]` 的 seal 语义保持不变。

`PETAL_SEARCH_URL` 与 headers 同步改用 live read，避免同一次 Petal 请求中 endpoint
读取旧快照而 headers 读取新 live 值，造成配置不一致。

## 拒绝的方案

- 全局修改 `read_env()`：会改变大量共享配置的请求一致性语义，blast radius 过大。
- 请求级 atomic live snapshot：超出本次缺陷目标；当前 URL 与 headers 是两次读取，
  极端并发 reload 下可能读到不同时刻的值。
- `parse_default_headers` 增加 `ast.literal_eval`、`materialize_sync_env` 序列化加固：
  与本次根因无关，属于独立兼容性增强，应另行评审。
- `read_env_live` 键白名单：可防误用，但会继续扩大共享 API 行为与测试面；当前仅
  两个 Petal 专用生产调用点，先以 spec 例外清单约束。

## 验证

新增回归测试覆盖：

- sealed overlay 下，`read_env_live()` 读取 staged tip 的新值。
- sealed overlay 下，普通 `read_env()` 保持旧快照语义。
- overlay 绑定期间 live tip miss 视为 unset，不回退 sealed overlay / 进程环境。
- overlay 未绑定时保留冷启动进程环境回退。
- Petal URL / headers 在 sealed overlay 下读取 reload 后最新值。

命令：

```powershell
pytest tests/unit_tests/common/test_local_env_config.py tests/unit_tests/agents/harness/test_web_search.py -q
```

## 已知遗留

- `read_env_live()` 是通用共享入口，当前靠 spec 与调用点约束适用范围；后续如需
  硬性防误用，可增加白名单。
- URL 与 headers 不是一次原子读取；若未来要求请求级强一致，需要单独设计 live
  配置快照。
