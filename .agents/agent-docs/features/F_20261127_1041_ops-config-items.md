---
id: F_20261127_1041_ops-config-items
title: ops.config.items 特性设计
type: F
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/extensions_v2
  - jiuwenswarm/server/runtime/gateway_adapter
files:
  - jiuwenswarm/extensions_v2/event_types/config.py
  - jiuwenswarm/extensions_v2/handler_ops/config.py
  - jiuwenswarm/extensions_v2/config_extension/catalog.py
  - jiuwenswarm/extensions_v2/config_extension/dispatch.py
  - jiuwenswarm/extensions_v2/config_extension/native.py
  - jiuwenswarm/server/runtime/gateway_adapter/config_adapter.py
designs: []
design_note: 用户明确要求实现此前讨论的两事件与三只读方法；此前方案未分配仓库 D 标识，本次不虚构审批记录。
---

# ops.config.items 特性设计

## 1. 范围、状态与依据

`await ops.config.items(scope="effective")` 返回当前公开、可展示的配置行数组，逐项触发 config.describe。
用于插件自己的设置菜单、命令查询、说明展示，不要求 UI 渲染，不改变原前端面板。
主责规约：[S_21](../specs/jiuwenswarm/extensions_v2/S_21_config-extensions.md)。行字段对齐 Claude ConfigRow 与 $.config.list 的展示/值分工，方法名按本项目 items。

## 2. 能力说明与 Claude 对照

插件要列出可以向用户说明的设置时，用一次 items 得到名称、说明、值、类型、选项、归属与锁状态。
另一插件可以订阅 describe 改名称或隐藏行。平台在返回后附加可信值，describe 不能把 API_KEY 加进目录。
区别是本项目显式公开四项、增加 scope，没有导入 Claude 的通用插件 userConfig 注册或自定义写方法。

## 3. 事件契约

本方法触发 [config.describe](F_20261127_1041_config-describe.md)，每次调用对每个公开候选项触发一次。
被隐藏的行在事件结束后省略；普通 value/snapshot 不受展示隐藏影响。

## 4. 方法契约

```python
rows = await ops.config.items()        # 生效视图
saved_rows = await ops.config.items("user")
for row in rows:
    label, value = row["label"], row["value"]  # 交给插件自己的展示或业务逻辑
```

scope 是可选 str，默认 effective。填写与读取规则：

| scope | 应填写什么 | 实际读取与作用 |
| --- | --- | --- |
| `effective` | 默认值；需要当前实际生效设置时填写 | 复用原 `get_config()` 的环境引用解析、规范化及运行配置优先级，再投影公开字段；不另建四层合并算法 |
| `user` | 需要当前宿主用户 YAML 层视图时填写 | 读取原 `get_config_raw()` 并补公开字段默认值，不解析环境引用；无法形成该字段合法标量的原始表达式使用默认值 |
| `project` / `session` | 本期不要填写 | 明确抛出 `ConfigExtensionError("UNSUPPORTED_SCOPE")`，不偷偷回退 `user` |

`scope` 不是用户 ID、项目路径或配置文件名，不能用它切换到其他用户目录。这里的 user 指当前
AgentServer 已绑定的配置目录；企业版账号偏好不混入此 YAML 视图。


返回 `list[ConfigRow]`；没有可展示字段时返回 []，不返回 None。数组中每一行：

| 字段 | 类型 / 谁填写 | 代表什么、如何使用 |
| --- | --- | --- |
| `key` | `str`；平台 | 稳定配置标识，供 `value(key)` 使用；不要把可改的 label 当 key |
| `label` | 非空 `str`；describe 链 | 人可读名称，例如“上下文引擎”；最多 128 字符 |
| `description` | 可省略 `str`；describe 链 | 帮助说明，最多 2048 字符；省略表示不显示说明 |
| `kind` | `"boolean"` / `"choice"`；平台 | 如何填写：布尔开关或字符串选项；本期不编辑复杂对象 |
| `value` | `bool` / `str`；平台 | 这次 scope 快照中的值，不是 Handler 自报值 |
| `options` | `list[str]`；仅 choice | 合法选项，如语言 `["zh", "en"]`；布尔行不带该字段 |
| `provider` | `{plugin,tier}`；平台 | 字段归属，首批 `{plugin:"engine",tier:"core"}`；不是当前调用插件 |
| `isLocked` | `bool`；平台 | 原生字段是否支持编辑；首批公开项为 False。False 不保证后续 USER 保存策略一定放行 |


行例子：

```json
{"key":"rsi.enabled","label":"RSI","description":"Enable the native RSI entry.",
 "kind":"boolean","value":false,"provider":{"plugin":"engine","tier":"core"},"isLocked":false}
```

读值在 describe 前一次取出，避免一份目录里每行值各自读取不同瞬间；展示 Handler 如发生写入，
本次目录仍代表读取时的快照，下次调用才刷新。返回为新数组/普通对象，不写回共享配置。
describe 内递归调用 items 抛 CONFIG_REENTRANT；未支持 scope 抛 UNSUPPORTED_SCOPE。

## 5. 接入与兼容方案

```text
业务事件 → USER await ops.config.items(scope)
 → ops.build 登记的方法代理 → 原配置读取 → 公开值快照
 → 每项 config.describe：USER → 可选 BUILTIN → 默认 CORE → 逐层返回
 → 按 isHidden 筛选 → 附加可信行字段 → 数组返回原 USER
```

本方法使用当前 Ops 的上下文继续 describe；该上下文可以为 None，因为宿主配置不是会话目录查询。
Handler 不自行收集 SessionContext，不指定其他用户的配置路径。卸载展示插件后名称/隐藏策略撤销，
共享方法与默认目录仍可用。原 config.get RPC 与前端消费结构保持不变。

| 稳定配置 key | 返回类型 / 缺省值 | 原生写入口与参数 | 含义与边界 |
| --- | --- | --- | --- |
| `preferred_language` | `str`，`"zh"` | `locale.set_conf`，`preferred_language` | 宿主语言，`zh` 中文、`en` 英文；企业版语言归 Gateway/用户偏好服务，不发布为宿主 YAML 项 |
| `react.context_engine_config.enabled` | `bool`，`False` | `config.set` / `config.save_all` 的 `context_engine_enabled` | 原上下文引擎开关，非本扩展自动执行压缩 |
| `rsi.enabled` | `bool`，`False` | 同上，`rsi_enabled` | 原 RSI 入口开关 |
| `rea.enabled` | `bool`，`False` | 同上，`rea_enabled` | 原 REA 入口开关 |

字段由 `config_extension/catalog.py` 显式发布。未登记字段，包括模型密钥、令牌、任意 YAML 路径，
不因名称看起来普通就自动开放。首批不包含通用插件 `userConfig` 字段注册，也不新增项目/会话配置存储。


## 6. 验收与未决问题

定向验收入口：`tests/unit_tests/extensions_v2/test_config_extensions.py`。
测试通过真实 assembly/Registry 安装 USER 包，使用隔离的真实 YAML 文件，调用公开 Ops、原生命令和
`ConfigAdapter.handle()`；写后独立用 YAML 解析器读取文件，并检查原热更新回调次数。
并发批量、取消、伪造结果、隐藏与只读投影、无会话读取和卸载均有覆盖。
最终实际命令、数量与结果见本文末尾“验证记录”；未记录为通过的场景不能据此声称已验收。
本次不涉及模型调用、浏览器绘制或完整 Gateway/Worker OS 进程验收。


覆盖默认目录、展示改写、隐藏不等于读权限、保护字段、一次快照、无会话、重入与真实示例 ZIP。
验证命令 `/config_probe` 可直接观察 value/items/snapshot 返回；RSI 行标签为 RSI (config probe)。

### 实测记录（2026-11-27）

候选基于官方 develop `8b3b92a7138d20f1b884554e8d5f816f98082521`；沿用声明的
Core `1b066729e6159aa149fe9bdc2c31ec1778e2a681`、Gateway `bb699e4362f983872772b1e369e0a00638c999fe`，
Python 3.11.15。未更改依赖锁或全局运行配置。

```text
python -m pytest tests/unit_tests/extensions_v2/test_config_extensions.py
  tests/unit_tests/extensions_v2/test_assembly.py
  tests/unit_tests/extensions_v2/test_session_ops.py
  tests/unit_tests/extensions_v2/test_session_query_events.py
  tests/unit_tests/extensions_v2/test_session_authorize.py
  tests/unit_tests/server/test_config_adapter_permission_reload.py
  -q -o addopts= --asyncio-mode=auto -p no:cacheprovider
```

上面为跨行展示，执行时将路径合并为同一条命令，并在正确依赖环境中运行。
实际结果：**149 passed**，其中本次配置文件 **38 项**，既有装配、查询、授权及热更新回归 **111 项**。
包括真实示例 ZIP 安装与 `/config_probe` 命令、原 ConfigAdapter 写入、独立 YAML 读回、
同一无 matcher 策略逐项检查批量请求、提交前取消清理、保护字段、全局无会话入口与卸载。
热更新使用被调用次数可观测的受控回调；不将此记录扩大为真实模型、前端绘制或完整 OS 进程验收。

## 7. 参考资料

[S_21](../specs/jiuwenswarm/extensions_v2/S_21_config-extensions.md)、[config.describe](F_20261127_1041_config-describe.md)、[value](F_20261127_1041_ops-config-value.md)。实现：`handler_ops/config.py`。
Claude 固定声明：[ConfigRow / config.list](https://github.com/anthropics/claude-code/blob/e2c963a886e345ca22d5e882ff8a63e3192529d0/mods/types/claude-code.d.ts)。
