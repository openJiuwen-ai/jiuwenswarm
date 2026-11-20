# session.receive 特性设计

| 项 | 值 |
| --- | --- |
| 编号 | F_26 / session-receive |
| 归档日期 | 2026-11-02 |
| 后续修订 | 2026-11-03，按 session.append 的写法补齐输入对象示例及 origin 对象说明，字段与运行行为不变 |
| 关联计划 | [EXTV2-24 / 跨会话接收入队前扩展](../designs/extensions-v2-development-plan.md#extv2-24-跨会话接收入队前扩展) |
| 主责规约 | [S_14](../specs/S_14_session-receive.md) |

## 1. 范围、状态与依据

本特性让目标会话在持久消息入队前观察、改写或消费本次跨会话投递。真实入口为原 SessionMessageService.send_message，平台完成归属校验和源 session.send 后，使用**目标会话** SessionContext 触发接收链。无新增前端、Ops、模型轮次或第二份消息队列，交付基于已合入 !99 的 Server `e847fb045981`；验证使用 已合入 Core PR !110 的实现提交 `4aceb5cda1dc`，本次不新增 Core API。实现已完成，验证记录见第 6 节。

参考固定 Claude Code Mods 2.1.277、提交 `80c6959d28c4c30ecd14fc1cdf172f19d9adad81` 的 SessionReceiveInput / Origin / Event / Result，优先对齐 origin、text、event 和 consumed。Python 字段用 snake_case，不能直接复制 TypeScript 对象当成事件输入。

## 2. 能力说明与 Claude 对照

例如会话 B 是发布助手，只接收带 `[release]` 的消息。USER 可以把批准的正文加上统一提示后传给 next；其他消息返回 `SessionReceiveConsumed(consumed="不是发布请求，已由插件处理")`，目标不入队、无新增历史、也不启动模型。消费表示本次投递已由插件接管，不表示删除整个会话。

| 项 | 本次设计 | 与固定 Claude 定义的关系 |
| --- | --- | --- |
| 输入 | origin、text、可选 event | 相同职责：可信来源、正文、平台确认的结构化外部事件 |
| 来源 | SessionReceiveOrigin(kind=...) | 对齐对象形态，matcher 使用 origin.kind；当前生产者仅生成 peer-send-message |
| 改写 | 仅 text，传新 Event 给 next | 改写进入原队列的文本，origin/event 固定 |
| 放行 | SessionReceiveResult(text=...) | 返回实际入队正文，next 在真实持久队列保存完成后返回 |
| 消费 | SessionReceiveConsumed(consumed=...) | next 前显式消费；不入队、不记录、不执行目标模型 |
| 宿主范围 | 当前持久跨会话服务 | 不冒充 Claude 的全部 bridge/relay/Remote Control 接入已实现 |

## 3. 事件契约

### 3.1 触发前后与粒度

```text
源会话现有发送工具 → 平台授权 → 源 session.send USER / CORE
  → 平台从已校验目标元数据构造目标 SessionContext
  → original = SessionReceiveInput(origin=peer-send-message, text=发送链放行正文)
  → await Worker bus.trigger(Event("session.receive", original), target_context)
  → USER：查看 text、调用目标的已有只读 Ops；改写或返回 consumed
  → await next_handler(Event("session.receive", rewritten))
  → 其余 Handler → session_receive_core
  → CORE 复验目标上下文、origin/event 和正文，执行原队列 continuation
  → 原锁内复查源与目标归属/模式 → SQLite enqueue / 幂等去重 → 原通知及后台消费者
  → CORE 返回实际入队 text；私有状态保留真实队列回执
  → 真实回执沿源 send 链返回工具；目标后台再异步执行
```

本事件无新增 BUILTIN；共享 Builder 仍只负责把平台能力绑定给本次实际 Handler。源 send 与目标 receive 的 USER 使用不同的会话身份：在 receive 中调用 ops.session.cwd() 查询的是 B，不是 A。Core 在业务触发时绑定 ops.session_context，用户无需收集目录资料或调用 notify_context_usage。

### 3.2 输入：每个字段表达什么、由谁填写

导入 `jiuwenswarm.extensions_v2.event_types.session_receive` 中的公开 dataclass。

下面示意平台在目标会话上提供的输入对象。用户通过 `event.event_input` 读取；来源由平台确定，改写时只替换 `text`。

```python
SessionReceiveInput(
    origin=SessionReceiveOrigin(kind="peer-send-message"),
    text="请检查订单模块，联系电话 [phone]",
    event=None,
)
```

`origin` 是完整来源对象，`origin.kind` 是其中的来源类型字段；两行说明的是对象及其成员，不需要填写两份来源信息。

| 字段 | 类型 / 是否必有 | 准确含义 | 应填写什么、例子和用途 | Handler 权限 |
| --- | --- | --- | --- | --- |
| origin | SessionReceiveOrigin，必有 | 平台判定这次输入从何而来 | 平台填写，当前为 kind=peer-send-message；用于规则匹配，不从正文里的声明推断来源 | 只读 |
| origin.kind | str，必有 | 来源分类，非源会话 ID，也非包 ID | bridge、task-notification、scheduled-trigger、peer、peer-send-message、projects-relay、slack-ping、unclassified；本次真实入口只产生 peer-send-message | 只读，可匹配 |
| text | str，必有 | 目标将读取的完整文本，也是将被保存的正文 | 平台先带入发送链放行文本；改写者填写完整新正文，例如 `[release] 请检查版本`；非空白，UTF-8 不超过 32768 字节 | 仅此项可改 |
| event | SessionReceiveEvent 或 None，默认 None | 平台确认的一次结构化外部事件，不是 EventBus 的 Event 包装，也不是任意用户 JSON | 普通跨会话文本一律 None；即使正文看起来像 webhook JSON 也不自动解析 | 只读 |

#### event 的全部字段

以下为对齐 Claude 保留的类型，**当前跨会话生产者不构造 event**。需要后续平台接入先声明可信来源，用户不能在 receive Handler 中把普通文本升级成可信外部事件。

| 字段 | 类型 / 是否必填 | 含义、应该填写什么 |
| --- | --- | --- |
| source | str，必填 | 产生事件的服务，如 github；不是用户填写的用户身份 |
| kind | str，必填 | 服务事件名，如 pull_request.closed；不是接收事件名 session.receive |
| data | dict[str, JSON]，必填 | 外部事件 JSON 正文，如 {"pr":"acme/app#12","outcome":"merged"}；平台保留其原结构 |
| untrusted_keys | tuple[str, ...]，必填 | data 中第三方可写自由文本的键名，如 author/comment；空 tuple 代表无此类字段；对应 Claude untrustedKeys |

源/目标会话、真实用户、请求和幂等身份保留在平台调用状态。事件不接受自行指定的目标 SessionContext，改 text 不能改变目标、跳数或用户权限。

### 3.3 输出：放行与消费

| 返回对象 / 字段 | 何时返回 | 应填写什么、含义与使用方式 |
| --- | --- | --- |
| SessionReceiveResult.text | CORE 已实际入队或命中既有幂等行 | 实际消息行的正文；USER 通常直接返回 await next_handler，不应自行伪造成功 |
| SessionReceiveConsumed.consumed | USER 在 next 前决定接管输入 | 填写去首尾空白后 1～512 字符原因，如 `重复的构建通知，已处理`；不再调用 next |

next 可能继续到下游 USER 的 consumed 分支；调用者若要读取 result.text，应先用 isinstance(result, SessionReceiveResult) 确认放行类型。两种输出互斥；普通 dict、None、空消费原因或假的 text 回执均不是合法放行。仅在本事件 CORE 尚未开始时允许 consumed，调用 next 后不能假定仍有消费机会。CORE 后再返回 consumed 无法撤销已经保存的行；平台仍使用真实回执。原业务调用者收到 accepted=False、code=SESSION_RECEIVE_CONSUMED 和 consumed/error，而不是虚假的队列成功。上游 send 拒绝时本事件完全不触发。

### 3.4 注册、筛选、改写示例

```python
from dataclasses import replace
from openjiuwen.core.events import Event
from jiuwenswarm.extensions_v2.event_types.session_receive import SessionReceiveConsumed

class Extension:
    def register(self, registrar):
        registrar.register_event_handler(
            "session.receive", self.on_receive, {"origin.kind": "peer-send-message"}, "user",
        )

    async def on_receive(self, event, ops, next_handler):
        if not event.event_input.text.startswith("[release]"):
            return SessionReceiveConsumed(consumed="不是发布请求，已由插件处理")
        text = event.event_input.text + "\n请先核对测试结果。"
        return await next_handler(Event(event.event_type, replace(event.event_input, text=text)))

def create_extension():
    return Extension()
```

matcher 为等值条件合取，嵌套字段用 origin.kind。Handler 匹配在当前链开始时固定；改写 text 不重跑 matcher。这里只继续 next，不重新调用 send，避免重复投递或递归。

## 4. 方法契约

### 4.1 平台接收入口与共享 Ops

本次不增加 ops.session.receive()。接收由原跨会话服务自动触发；只安装上述用户包即可参与，不要求用户改业务方法。目标 USER 可以调用已有七项只读 Ops；这些查询另起同名查询事件，不产生新的接收投递。

平台内部 _dispatch_receive(bus, context, text, commit) 使用可信目标 context、发送链放行的 text 构造 SessionReceiveInput，并绑定真实 enqueue continuation。只有 continuation 被 CORE 调用才入队；只有原 SQLite 服务可以产生真实 accepted 回执。裸 bus.trigger 不能创建该私有调用，不能绕过源/目标授权。

### 4.2 兼容与迁移

此前没有 receive Handler；无 Handler 时照旧入队。消息 Schema、幂等键、投递状态与目标执行逻辑保持。接收规则可有意改变本次入队正文或消费本次输入；不补扫、重写、删除已在队列/历史中的旧消息。

## 5. 接入与兼容方案

event_types/session_receive.py 只维护类型、校验和注册；平台触发位于 server/runtime/session/session_message_events.py；CORE 位于 event_handlers/session_receive.py。真实保存、通知及后续模型输入仍复用 SessionMessageService / SessionMessageStore。

发送方和接收方上下文分别构造，正文及 event 均使用独立副本；不持目标锁执行 USER。队列锁内重新核对源与目标的归属及支持模式，避免 Hook 等待期间会话删除或换归属后继续投递。每次调用仅提交一次。CORE 开始前标记 attempted；底层失败/取消被本次私有状态保存，重复 next 继续抛出原失败，不重新执行入队，USER 不能捕获后用 consumed 掩盖失败。新业务调用仍按原幂等规则重试。子任务在调用退出后不能使用过期状态提交；卸载仅撤销用户 Hook，不删除已提交队列。

选择在原队列前执行接收链，才能让 consumed 阻止本次入队、通知及目标执行。只在模型开始前加通知会造成被消费输入已经出现在队列中，因此未采用。同步完成整轮目标模型会阻塞发送方，也未采用。

## 6. 验收与未决问题

2026-11-03 仅修订文档：核对 feature/spec 连续编号、文件名、双向引用及 session.append 七节结构，并补齐第 3.2 节的输入对象示例和对象成员说明。本次不改运行代码、不重跑业务测试；以下保留 2026-11-02 的实现验证范围与结果。

2026-11-02 已完成以下验证，执行环境为 Windows、Python 3.11、Server `e847fb045981` 加本次实现、Core `4aceb5cda1dc`（与已合入目标 `3cac40a97fd4` 的源码树一致）：

| 验证层次 | 已通过结果 | 证据与边界 |
| --- | --- | --- |
| 新增投递测试 | 42 项通过 | [test_session_delivery_events.py](../../../../tests/unit_tests/extensions_v2/test_session_delivery_events.py)：实际 Registry/Bus/Ops 和 SQLite，涵盖正文改写、拒绝/消费、保护字段、伪造结果、并发/重复 next、幂等、归属变化、重入、卸载及取消。会话元数据为测试夹具 |
| 相关回归 | 1514 项通过、2 项跳过 | extensions_v2、runtime、原 session_messaging、session_switch、history_append_record；两项跳过为本机不支持 symlink / named pipe，不列为通过 |
| 独立进程安装验收 | 通过 | 实际安装用户包和观察包 ZIP；从磁盘会话元数据生成 A/B 上下文，查询 cwd/root；独立 SQLite SELECT 核对脱敏后内容、拒绝/消费无行、卸载后恢复。后台执行回调收到实际保存正文，退出后装配及后台任务释放 |
| 取消/失败边界 | 通过 | 先复现入库后 USER 吞取消并伪装拒绝/消费、捕获存储错误后重复 next 的 3 项失败；修复后纳入上述 42 项，通过且不重做本次底层操作 |

```powershell
# 将当前 Server 源码与上述 Core 源码放入 PYTHONPATH 后，在 Server 根目录执行
python -m pytest -q -o addopts= --asyncio-mode=auto --no-cov -o log_cli=false tests/unit_tests/extensions_v2/test_session_delivery_events.py
python -m pytest -q -o addopts= --asyncio-mode=auto --no-cov -o log_cli=false tests/unit_tests/extensions_v2 tests/unit_tests/runtime tests/unit_tests/agentserver/test_session_messaging.py tests/unit_tests/agentserver/test_session_switch_characterization.py tests/unit_tests/agentserver/test_history_append_record.py
```

独立验收使用受控目标执行回调，未向模型服务发送请求，也未运行完整 Front/Supervisor/Worker 或浏览器链路；不将这些范围列为通过。示例 README 提供可安装包与三组触发输入。

限制：本轮生产入口仅覆盖原持久跨会话服务；普通 Web chat.send、cron、Webhook/Remote Control 和独立 AgentRuntime.send_session_message 未接入本事件。保留 Claude 其它 origin 枚举及 event 类型不代表这些渠道已实现。未验证项不列为通过。

## 7. 参考资料

- [S_14 主责规约](../specs/S_14_session-receive.md)、[发送特性](F_25_session-send.md)。
- [平台上下文与触发](../../../server/runtime/session/session_message_events.py)、[接收 CORE](../../event_handlers/session_receive.py)。
- [原平台消息服务](../../../server/runtime/session/session_message_service.py)、[原消息存储](../../../server/runtime/session/session_message_store.py)。
- [Claude 固定类型定义](https://github.com/anthropics/claude-code/blob/80c6959d28c4c30ecd14fc1cdf172f19d9adad81/mods/types/claude-code.d.ts)：SessionReceiveInput / Origin / Event / Result。
