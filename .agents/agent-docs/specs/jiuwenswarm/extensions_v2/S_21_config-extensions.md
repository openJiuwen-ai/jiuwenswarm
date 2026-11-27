---
id: S_21_config-extensions
title: 配置扩展事件与读取方法规约
type: S
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

# 配置扩展事件与读取方法规约

## 1. 范围与事实源

本文是两个配置事件与三个只读方法的公共契约。当前宿主为 AgentServer，使用原配置解析、原 ConfigAdapter
及原配置面板保存/热更新，不修改 Core/Gateway/前端/依赖锁。公开配置以显式目录为边界，配置文件仍为原权威。

## 2. 接口

| 接口 | 输入 | 输出 | 行为 |
| --- | --- | --- | --- |
| config.describe | key/label/description/isHidden/provider/scope | label/description?/isHidden | 每个公开目录行生成说明 |
| config.set | key/value/previous/provider/origin/scope | value 或 deny | 原生保存前处理一项候选值 |
| ops.config.value | key, scope="effective" | bool/str/None | 直接读取一个公开值，不触发事件 |
| ops.config.items | scope="effective" | list[ConfigRow] | 一次读值快照，逐项触发 describe，省略隐藏行 |
| ops.config.snapshot | 无参数 | dict | 直接读取 effective 公开嵌套投影 |

| 稳定配置 key | 返回类型 / 缺省值 | 原生写入口与参数 | 含义与边界 |
| --- | --- | --- | --- |
| `preferred_language` | `str`，`"zh"` | `locale.set_conf`，`preferred_language` | 宿主语言，`zh` 中文、`en` 英文；企业版语言归 Gateway/用户偏好服务，不发布为宿主 YAML 项 |
| `react.context_engine_config.enabled` | `bool`，`False` | `config.set` / `config.save_all` 的 `context_engine_enabled` | 原上下文引擎开关，非本扩展自动执行压缩 |
| `rsi.enabled` | `bool`，`False` | 同上，`rsi_enabled` | 原 RSI 入口开关 |
| `rea.enabled` | `bool`，`False` | 同上，`rea_enabled` | 原 REA 入口开关 |

字段由 `config_extension/catalog.py` 显式发布。未登记字段，包括模型密钥、令牌、任意 YAML 路径，
不因名称看起来普通就自动开放。首批不包含通用插件 `userConfig` 字段注册，也不新增项目/会话配置存储。


| scope | 应填写什么 | 实际读取与作用 |
| --- | --- | --- |
| `effective` | 默认值；需要当前实际生效设置时填写 | 复用原 `get_config()` 的环境引用解析、规范化及运行配置优先级，再投影公开字段；不另建四层合并算法 |
| `user` | 需要当前宿主用户 YAML 层视图时填写 | 读取原 `get_config_raw()` 并补公开字段默认值，不解析环境引用；无法形成该字段合法标量的原始表达式使用默认值 |
| `project` / `session` | 本期不要填写 | 明确抛出 `ConfigExtensionError("UNSUPPORTED_SCOPE")`，不偷偷回退 `user` |

`scope` 不是用户 ID、项目路径或配置文件名，不能用它切换到其他用户目录。这里的 user 指当前
AgentServer 已绑定的配置目录；企业版账号偏好不混入此 YAML 视图。


## 3. 事件与权限不变量

- CFG-01：describe 只采用 label/description/isHidden；key/provider/scope 不可改，值/类型/锁状态归平台。
- CFG-02：set 只改 value，保持类型并通过字段选项校验；key/previous/provider/origin/scope 与平台帧一致。
- CFG-03：origin 当前由 E2A 平台填 bridge，scope 固定 user；用户 params 不决定来源或目标用户。
- CFG-04：值、目录和快照只能返回显式公开字段；隐藏目录行不是读写授权控制，不开放任意 YAML 路径。
- CFG-05：读 Ops 可以没有会话；不得用 scope/key 选择别的配置目录。企业版账号语言仍归既有权威入口。
- CFG-06：set CORE 需要任务私有调用帧和同一上下文；直接 EventBus trigger 不能调用写入 continuation。
- CFG-07：USER 异常沿用 Core 故障隔离；显式策略拒绝用合法 deny，不依赖异常表达拒绝。
- CFG-08：无 USER 或卸载后，共享 CORE/Ops 保留；扩展宿主不可用时 ConfigAdapter 继续原路径。

## 4. 提交、批量与取消

同一原生请求的公开字段在独立事件任务中执行，继承宿主 ContextVars；所有字段到达 CORE 之前不执行
原保存。任何字段提前拒绝或伪造成功，整批不进入 writer。所有字段就绪后，仅调用一次原生写入/热更新。
不嵌套活动的同一个 USER Handler，不修改 Core 的 Handler 防重入接口，也不丢弃宿主租户上下文。

CORE 成功输出为 {value}，指原保存成功接纳的该字段值；原 AgentResponse 是平台私有真实回执。
post-next deny/改值/异常不伪造撤销；原保存返回失败时保留原错误响应。不新增原多文件保存的事务回滚。
取消会取消并等待所属字段任务；不承诺取消已经撤销提交。重复 next 不重复写入。

## 5. 返回字段与错误

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


describe 的 label 非空且最多 128 字符；description 可省略、最多 2048；isHidden 必须 bool。
set 输出严格二选一，deny 非空、最多 512 字符。前三个 Ops 的参数没有服务身份/配置文件路径。

| 错误 | 触发与效果 |
| --- | --- |
| INVALID_KEY | value 的 key 非 str/空/超长 |
| UNSUPPORTED_SCOPE | 非 effective/user，包含未接入 project/session |
| CONFIG_REENTRANT | items 递归或同一公开字段递归保存，阻止该调用 |
| INVALID_VALUE | 公共保存候选无法规范化或不符合字段约束，尚未执行原 writer |
| UNCOMMITTED_RESULT | Hook 未经 CORE 就返回假成功，拒绝写入 |
| CONFIG_SET_DENIED | USER 显式拒绝，原配置请求返回失败且不进入 writer |
| CONFIG_EXTENSION_ERROR | 最终扩展链失败，未进入 writer 时返回受控错误 |
| UNBOUND_INVOCATION | 缺少/伪造平台私有帧或上下文不符 |

## 6. 验证入口与边界

`tests/unit_tests/extensions_v2/test_config_extensions.py`，示例 `examples/config_probe`。
验证真实包安装、读 Ops、describe、ConfigAdapter、批量策略、原保存/回调、独立 YAML 读回与卸载。
源码记录最终实际数量；不把控制后端或测试回调扩大成真实模型、浏览器/OS 进程验收。

## 7. 特性与参考

- [config.describe 特性设计](../../../features/F_20261127_1041_config-describe.md)
- [config.set 特性设计](../../../features/F_20261127_1041_config-set.md)
- [ops.config.value 特性设计](../../../features/F_20261127_1041_ops-config-value.md)
- [ops.config.items 特性设计](../../../features/F_20261127_1041_ops-config-items.md)
- [ops.config.snapshot 特性设计](../../../features/F_20261127_1041_ops-config-snapshot.md)

原始分工方案由用户提出并授权开发。Claude 对照固定在 [mods/types/claude-code.d.ts](https://github.com/anthropics/claude-code/blob/e2c963a886e345ca22d5e882ff8a63e3192529d0/mods/types/claude-code.d.ts)，
借鉴展示/候选值/配置行分工；三个方法的本地命名、scope、显式公开字段过滤与批量门闩为本项目适配。
