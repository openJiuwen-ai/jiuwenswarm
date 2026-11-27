---
id: F_20261127_1041_ops-config-snapshot
title: ops.config.snapshot 特性设计
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

# ops.config.snapshot 特性设计

## 1. 范围、状态与依据

`await ops.config.snapshot()` 无参数，返回当前宿主有效配置的公开嵌套投影副本。
它复用原 resolver 的优先级，不另建配置存储；仅发布目录中的字段，敏感和未登记字段不返回。
主责规约：[S_21](../specs/jiuwenswarm/extensions_v2/S_21_config-extensions.md)。参考 Claude $.settings.read 的“有效配置快照”用途；过滤规则按用户要求收窄。

## 2. 能力说明与 Claude 对照

当插件一次需要多个公开设置做判断时，用一个 snapshot，避免逐项异步读取不同瞬间的配置。
例如查看 snapshot["rsi"]["enabled"] 与 snapshot["react"]["context_engine_config"]["enabled"]。
与 Claude 参考声明存在明确差异：本方法不过 settings.read 事件，不暴露完整 settings，不提供原对象引用。

## 3. 事件契约

不触发 config.describe、config.set 或新的 settings.read。入口由用户自己的业务 Handler 选择。
本期只有两个配置事件，不能把每个 Ops 名称推导为新增同名事件。

## 4. 方法契约

```python
settings = await ops.config.snapshot()
if settings["rsi"]["enabled"]:
    # 根据平台已生效开关选择插件行为
    pass
```

输入没有 key/scope/session_id 参数；固定读取 effective 视图。输出是独立 dict：

| 输出位置 | 类型 / 含义 | 消费示例 |
| --- | --- | --- |
| `preferred_language` | str，zh/en；企业版不发布 | 选择插件自身的说明语言 |
| `react.context_engine_config.enabled` | 嵌套 bool | 判断原上下文引擎开关 |
| `rsi.enabled` | 嵌套 bool | 判断 RSI 入口开关 |
| `rea.enabled` | 嵌套 bool | 判断 REA 入口开关 |

```json
{"preferred_language":"zh","react":{"context_engine_config":{"enabled":false}},
 "rsi":{"enabled":false},"rea":{"enabled":false}}
```

这些点分位置对应真实嵌套 dict，不是把点当成一个字面 key。未公开项直接省略，不返回掩码字符串，
也不把未知敏感值伪造成零。没有可公开项时为 {}；本期发布目录有宿主默认值，因此通常有上述项。
修改这个 dict 不持久化、不热更新，下一次调用仍读平台事实。该快照不是会话历史、模型上下文或配置备份。

## 5. 接入与兼容方案

```text
平台业务事件 → 插件 Handler → await ops.config.snapshot()
 → config.snapshot 方法代理注入当前 Ops
 → 在线程中一次调用原 get_config（环境解析、原优先级）
 → 按显式公开目录选值、补字段默认值 → 按原稳定路径构造新的嵌套 dict → 返回
```

| 稳定配置 key | 返回类型 / 缺省值 | 原生写入口与参数 | 含义与边界 |
| --- | --- | --- | --- |
| `preferred_language` | `str`，`"zh"` | `locale.set_conf`，`preferred_language` | 宿主语言，`zh` 中文、`en` 英文；企业版语言归 Gateway/用户偏好服务，不发布为宿主 YAML 项 |
| `react.context_engine_config.enabled` | `bool`，`False` | `config.set` / `config.save_all` 的 `context_engine_enabled` | 原上下文引擎开关，非本扩展自动执行压缩 |
| `rsi.enabled` | `bool`，`False` | 同上，`rsi_enabled` | 原 RSI 入口开关 |
| `rea.enabled` | `bool`，`False` | 同上，`rea_enabled` | 原 REA 入口开关 |

字段由 `config_extension/catalog.py` 显式发布。未登记字段，包括模型密钥、令牌、任意 YAML 路径，
不因名称看起来普通就自动开放。首批不包含通用插件 `userConfig` 字段注册，也不新增项目/会话配置存储。


环境变量和运行态优先级由原 get_config 决定。未建立项目/会话覆盖层，不承诺通用四层 settings 合并。
不用 SessionContext 作配置权威；读取在当前 AgentServer 已绑定的配置目录中进行，合法应用级 Handler
即使没有会话也可使用。describe 隐藏行不改变此方法的可读范围。

## 6. 验收与未决问题

定向验收入口：`tests/unit_tests/extensions_v2/test_config_extensions.py`。
测试通过真实 assembly/Registry 安装 USER 包，使用隔离的真实 YAML 文件，调用公开 Ops、原生命令和
`ConfigAdapter.handle()`；写后独立用 YAML 解析器读取文件，并检查原热更新回调次数。
并发批量、取消、伪造结果、隐藏与只读投影、无会话读取和卸载均有覆盖。
最终实际命令、数量与结果见本文末尾“验证记录”；未记录为通过的场景不能据此声称已验收。
本次不涉及模型调用、浏览器绘制或完整 Gateway/Worker OS 进程验收。


覆盖三个 Ops 在真实包中的共存、嵌套内容、未公开字段/密钥过滤、修改副本不影响原值、
企业版语言排除、effective 环境引用解析，以及不触发 describe 的负向检查。

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

[S_21](../specs/jiuwenswarm/extensions_v2/S_21_config-extensions.md)、[value](F_20261127_1041_ops-config-value.md)、[items](F_20261127_1041_ops-config-items.md)。实现：`config_extension/catalog.py`、`handler_ops/config.py`。
Claude 固定声明：[settings.read](https://github.com/anthropics/claude-code/blob/e2c963a886e345ca22d5e882ff8a63e3192529d0/mods/types/claude-code.d.ts)；差异仅在本文明确范围内对照，不声称复刻其全部 settings 权限。
