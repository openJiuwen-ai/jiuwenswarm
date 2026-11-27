---
id: F_20261127_1041_ops-config-value
title: ops.config.value 特性设计
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

# ops.config.value 特性设计

## 1. 范围、状态与依据

`await ops.config.value(key, scope="effective")` 读取单个公开配置的值，返回 bool/str 或 None。
通过现有 ops.build 登记，使用当前宿主配置目录，无需会话，不要求先调用 notify，也不触发新查询 Event。
主责规约：[S_21](../specs/jiuwenswarm/extensions_v2/S_21_config-extensions.md)。方法命名按用户要求保留 value；固定 Claude 声明未把它作为 $.config.get 的严格对应项。

## 2. 能力说明与 Claude 对照

当插件需要判断是否显示某个能力时，读取稳定 key，而不依赖可被 describe 改写的显示名称。
例如 `await ops.config.value("rsi.enabled")` 得到 False；False 是合法值，None 表示没有可公开返回值。
借鉴 Claude 配置目录的标量类型；scope 是本项目选择，不声称与 Claude 未核实的 get 签名完全相同。

## 3. 事件契约

此方法不触发 config.describe 或 config.set，也没有额外 config.value 事件。
订阅业务事件的 USER 主动调用它即可；无需订阅同名事件。

## 4. 方法契约

```python
value = await ops.config.value("rsi.enabled")
saved_view = await ops.config.value("rsi.enabled", "user")
unknown = await ops.config.value("models.api_key")  # None
```

| 参数 | 类型 / 默认 | 应填写什么、意义与作用 |
| --- | --- | --- |
| `key` | 非空 str，必填，最多 256 字符 | 填发布目录的稳定标识，如 rsi.enabled；不是面板别名 rsi_enabled、显示名称、文件路径或用户 ID |
| `scope` | str，默认 effective | 选择有效视图或用户 YAML 视图，不指定别的用户或项目 |

| scope | 应填写什么 | 实际读取与作用 |
| --- | --- | --- |
| `effective` | 默认值；需要当前实际生效设置时填写 | 复用原 `get_config()` 的环境引用解析、规范化及运行配置优先级，再投影公开字段；不另建四层合并算法 |
| `user` | 需要当前宿主用户 YAML 层视图时填写 | 读取原 `get_config_raw()` 并补公开字段默认值，不解析环境引用；无法形成该字段合法标量的原始表达式使用默认值 |
| `project` / `session` | 本期不要填写 | 明确抛出 `ConfigExtensionError("UNSUPPORTED_SCOPE")`，不偷偷回退 `user` |

`scope` 不是用户 ID、项目路径或配置文件名，不能用它切换到其他用户目录。这里的 user 指当前
AgentServer 已绑定的配置目录；企业版账号偏好不混入此 YAML 视图。


| 返回 | 类型 | 含义与使用例子 |
| --- | --- | --- |
| 已公开布尔值 | bool | 例如 False，按开关使用，不要用 `value or default` 混淆 False 与未知 |
| 已公开选项 | str | 例如语言 "zh"，按字段 options 解释 |
| 不可返回 | None | 未登记、敏感字段、当前版本/发行版不发布该项；不代表 False、零或空字符串 |

空 key / 非 str 抛 INVALID_KEY；未支持的 scope 抛 UNSUPPORTED_SCOPE。文件读取错误沿原读取层行为，
不把用户自报 dict 当可信配置。返回是独立标量，不提供 setter，不因修改本地变量写盘。

## 5. 接入与兼容方案

```text
平台业务事件 → 插件 Handler 收到当前 Ops
 → await ops.config.value(key, scope)
 → Builder 登记的 config.value 代理注入当前 Ops
 → 在线程中调用原 get_config / get_config_raw
 → 一次读取后取显式发布字段 → 返回该 key 的值或 None
```

| 稳定配置 key | 返回类型 / 缺省值 | 原生写入口与参数 | 含义与边界 |
| --- | --- | --- | --- |
| `preferred_language` | `str`，`"zh"` | `locale.set_conf`，`preferred_language` | 宿主语言，`zh` 中文、`en` 英文；企业版语言归 Gateway/用户偏好服务，不发布为宿主 YAML 项 |
| `react.context_engine_config.enabled` | `bool`，`False` | `config.set` / `config.save_all` 的 `context_engine_enabled` | 原上下文引擎开关，非本扩展自动执行压缩 |
| `rsi.enabled` | `bool`，`False` | 同上，`rsi_enabled` | 原 RSI 入口开关 |
| `rea.enabled` | `bool`，`False` | 同上，`rea_enabled` | 原 REA 入口开关 |

字段由 `config_extension/catalog.py` 显式发布。未登记字段，包括模型密钥、令牌、任意 YAML 路径，
不因名称看起来普通就自动开放。首批不包含通用插件 `userConfig` 字段注册，也不新增项目/会话配置存储。


读取无额外缓存，下一次调用可观察原配置写入；不叠加虚构的 project/session 层。
所有扩展可以读取本公开目录，不能通过此方法获取宿主凭据；不是 Python 扩展沙箱的完整安全承诺。
普通辅助函数可使用 Handler 传入的有效 Ops，任意后台函数没有自动创建 Ops 的机制。

## 6. 验收与未决问题

定向验收入口：`tests/unit_tests/extensions_v2/test_config_extensions.py`。
测试通过真实 assembly/Registry 安装 USER 包，使用隔离的真实 YAML 文件，调用公开 Ops、原生命令和
`ConfigAdapter.handle()`；写后独立用 YAML 解析器读取文件，并检查原热更新回调次数。
并发批量、取消、伪造结果、隐藏与只读投影、无会话读取和卸载均有覆盖。
最终实际命令、数量与结果见本文末尾“验证记录”；未记录为通过的场景不能据此声称已验收。
本次不涉及模型调用、浏览器绘制或完整 Gateway/Worker OS 进程验收。


覆盖无会话调用、False 与 None、未公开/敏感字段、scope 错误、effective 环境解析与 user 原始视图。
不把首批四项的通过等同于任意宿主配置均已开放。

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

[S_21](../specs/jiuwenswarm/extensions_v2/S_21_config-extensions.md)；`handler_ops/config.py`、`config_extension/catalog.py`；原 `common/config.py`。
相关：[items](F_20261127_1041_ops-config-items.md)、[snapshot](F_20261127_1041_ops-config-snapshot.md)。Claude 参考声明：[配置与设置类型](https://github.com/anthropics/claude-code/blob/e2c963a886e345ca22d5e882ff8a63e3192529d0/mods/types/claude-code.d.ts)，仅作对照边界依据。
