---
id: F_20261127_1041_config-describe
title: config.describe 特性设计
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

# config.describe 特性设计

## 1. 范围、状态与依据

实现位置为 AgentServer，复用现有 Core EventBus、Registry 与 Ops Builder，不修改 Core。
`config.describe` 在生成公开目录一行时触发，让 USER 调整名称、说明与隐藏状态；不读取/改写配置值。
主责规约为 [S_21](../specs/jiuwenswarm/extensions_v2/S_21_config-extensions.md)。对照固定 Claude 声明中的 `config.describe`，scope 是本项目附加字段。

## 2. 能力说明与 Claude 对照

插件通过订阅本事件，把例如 `rsi.enabled` 显示成“项目实验入口”，或从目录隐藏这一行。
平台在事件前已选定公开字段和值，返回后只采用展示字段，值、类型、归属和锁状态仍由平台组合。
沿用 Claude 的 key/label/description/isHidden/provider 分工；首期只提供已登记宿主字段，
不承诺 Claude 的通用插件配置注册，也不修改现有前端设置面板。

## 3. 事件契约

### 触发点与完整调用链

```text
平台触发 ui.ready / server.run_command / 其它业务事件
 → 插件 USER 主动 await ops.config.items(scope)
 → 平台一次读取该 scope 的公开值快照
 → 对每个公开字段 bus.trigger(Event("config.describe", input), 原 Handler 的 context)
 → 匹配 USER → 可选 BUILTIN → config_describe_core
 → 默认返回展示字段 → USER 的 next 之后可加工 → 回到 items
 → 平台省略隐藏行，附加可信 value/kind/provider/isLocked → 返回目录数组
```

每次 items 调用，每个候选字段触发一次。value/snapshot 不触发；首次加载包本身不触发。
无会话的合法 Handler 也可调用，CORE 允许 context 为 None，但要求存在本次平台私有描述帧。

### 输入字段

Handler 接收 Mapping，可用 `event.event_input["key"]`；不是任意新增属性的对象。

| 字段 | 类型 / 来源 | 填写什么、含义与作用 | 可否修改 |
| --- | --- | --- | --- |
| `key` | `str`；平台 | 稳定标识，如 `rsi.enabled`，用于 matcher 找到该行 | 固定 |
| `label` | `str`；平台默认 | 展示名称；插件可替换为清晰的人可读名称 | 可改，非空，最多 128 字符 |
| `description` | `str`；平台默认 | 对值的解释，如“true 开启 RSI 入口” | 可改，最多 2048 字符 |
| `isHidden` | `bool`；默认 False | True 仅表示从本次目录省略该行，不是读写权限 | 可改 |
| `provider` | `{plugin,tier}`；平台 | 字段归属，首批 engine/core；不能填写当前插件冒充字段所有者 | 固定 |
| `scope` | `effective` / `user`；调用者选定 | 正在为哪个读取视图生成说明；不是写入目标 | 固定 |

### 输出字段

只返回 `{label, description?, isHidden}`。label 与 isHidden 必填；description 可省略。
不要附加 key/value/provider/isLocked。CORE 默认原样返回输入的三个展示字段。
USER 可以直接返回合法展示对象，或者在 next 之后加工；非法 Hook 按 Core 既有规则隔离，
最终仍非法的结果会令 items 失败。嵌套调用 items 触发 CONFIG_REENTRANT；value/snapshot 可以使用。

```python
def register(self, registrar):
    registrar.register_event_handler("config.describe", self.describe, {"key": "rsi.enabled"})

async def describe(self, event, ops, next_handler):
    result = await next_handler(event)
    return {**result, "label": "项目实验入口", "description": "开启前请确认测试环境", "isHidden": False}
```

## 4. 方法契约

本事件由 `ops.config.items(scope="effective")` 驱动，方法本身见 [items 特性](F_20261127_1041_ops-config-items.md)。
使用配置查询无需订阅 describe；只有想改变目录说明才订阅。不存在要求用户自行填写 SessionContext 的参数。

## 5. 接入与兼容方案

bootstrap 注册事件和 CORE；ops.build BUILTIN 登记 config 命名空间。items 生成任务私有描述帧，
CORE 核对 key/provider/scope、当前上下文和展示字段。框架冻结事件输入，插件需要改输入时构造新 Event。
值及目录元数据来自同一次公开投影；展示隐藏不删除配置、不改变 value/snapshot 的可读范围。
卸载只撤销该插件 USER，不撤销共享 Ops/CORE。已有 config.get 扁平 RPC 保持原结构，不自动增加目录字段。

## 6. 验收与未决问题

定向验收入口：`tests/unit_tests/extensions_v2/test_config_extensions.py`。
测试通过真实 assembly/Registry 安装 USER 包，使用隔离的真实 YAML 文件，调用公开 Ops、原生命令和
`ConfigAdapter.handle()`；写后独立用 YAML 解析器读取文件，并检查原热更新回调次数。
并发批量、取消、伪造结果、隐藏与只读投影、无会话读取和卸载均有覆盖。
最终实际命令、数量与结果见本文末尾“验证记录”；未记录为通过的场景不能据此声称已验收。
本次不涉及模型调用、浏览器绘制或完整 Gateway/Worker OS 进程验收。


| 场景 | 操作与预期 |
| --- | --- |
| 默认目录 | items 返回四项（企业版三项），名称取平台默认 |
| 改写与隐藏 | 安装 USER 改 label 并隐藏 RSI，items 返回三项；value 仍可读 RSI |
| 固定身份 | Hook 试图把 key/provider 换成私有字段，不会暴露私有项 |
| 无副作用 | describe 后独立读取 YAML，配置值不变 |
| 重入 | describe 内调用 items 明确拒绝，外层目录仍可继续 |

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

公共契约：[S_21](../specs/jiuwenswarm/extensions_v2/S_21_config-extensions.md)；相关方法：[items](F_20261127_1041_ops-config-items.md)。
实现：`event_types/config.py`、`event_handlers/config.py`、`config_extension/dispatch.py`、`handler_ops/config.py`。
Claude 固定声明：[ConfigDescribeInput/Result](https://github.com/anthropics/claude-code/blob/e2c963a886e345ca22d5e882ff8a63e3192529d0/mods/types/claude-code.d.ts)。
