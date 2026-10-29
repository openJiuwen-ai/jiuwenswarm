# 会话轮次与用量示例 / Session turns and usage

## 中文

本包是用户侧代码，订阅已有 `context.usage.updated`，调用
`await ops.session.turns()` 和 `await ops.session.usage(breakdown=True)`，
写宿主日志后继续原 CORE 回执。用户无需修改平台业务方法、创建 EventBus 或自行收集上下文。

完整字段、返回值、注册示例和验收场景见 [F_21 特性设计](../../docs/features/F_21_session-usage-ops.md)
与 [S_11 规约](../../docs/specs/S_11_session-usage-ops.md)。两项 Ops 均直接读取，不触发另一条查询事件链。

在本目录将 manifest.yaml 与 extension.py 放在 ZIP 根目录：

```powershell
Compress-Archive -LiteralPath .\manifest.yaml, .\extension.py -DestinationPath .\session-usage-info.zip
```

将 ZIP 放在服务能读取的位置，使用已有命令：

```text
/extension install "D:\extensions\session-usage-info.zip"
/extension query example.session-usage-info
```

正常发送消息，有效原生回合通知用量时日志出现 `session usage:`。turns 是已落盘的主会话用户输入去重数，
不是模型调用、工具次数或 AgentLoop turn.start 序号。usage.context 是最后一次观察到的模型输入窗口占用，usage.turn 是本轮 provider 消耗，
两者不能相加或当成累计账单。context_window.occupancy_rate 是比例值（0.2 表示 20%），measurement 包含估算/实测来源，
`breakdown=True` 才带已有 context.parts 分项；旧数据可能只有 turn。turn 缺失计数为 None，携带的 0 保留。
Core 窗口/类别模型也有默认零值，需结合 measurement/estimated 判断来源，不单凭数值 0 推断已实测。

```text
启动：ops.build 的 CORE 创建 Builder → session BUILTIN 登记 turns/usage → Core 保存能力目录
运行：原生请求末尾有效用量 → notify_context_usage 构建快照并绑定 SessionContext
  → 异步 trigger(context.usage.updated, context) → 用户 on_usage 获得当次 Ops
  → await turns()：直接读取已提交主历史并去重
  → await usage(breakdown=True)：直接读本次宿主快照副本，保留已有 parts
  → 用户写日志 → next_handler(event) → 原用量 CORE 返回 accepted/request_id
```

通知按每个有有效 total_tokens 的原生请求末尾汇总发布，不按每次模型/工具调用或每个 Token 发布。
Builder 负责登记方法，业务时查询不再次触发 ops.build；本项没有新增查询 CORE。


任意其它已绑定有效 SessionContext 的 Handler 也能调用这两个共享 Ops：turns 和无宿主快照时的 usage
从 session_id 对应的已提交历史读取，用户不必自行调用 notify。会话存在但无输入时 turns=0，未知会话为 None。
宿主用量通知提供本次快照，避免异步通知时读到上一轮尚未更新的 summary；事件载荷字段不会覆盖 Ops 的资料。
查询不重新测量、不调用模型、不触发 session.measure，也不依赖 PR04 的 messages/append。

usage 返回 schema_version/session_id/source/context/turn，source 区分 host/history，两部分都无观测时返回 None。
宿主两个分量独立校验；一个坏分量不丢另一个有效分量，明确的全空快照不回读上一轮。
参数 breakdown 只接受 bool，数字/字符串会报 TypeError；没有 SessionContext 报 RuntimeError。
turns 和 usage 分别读取，不承诺两次调用是同一个原子快照；改写事件载荷不会覆盖可信会话或宿主快照。

读取在线程中等待既有历史写入边界，最久 10 秒，扫描历史 O(N)。损坏、I/O 或超时抛
SessionStatisticsError，用户可捕获后自行降级；示例不提供前端 UI。历史截断后轮次按保留分支计算，
压缩摘要和扩展追加不额外增加轮次。卸载只移除用户 Handler，不修改平台事实。

```text
/extension reload example.session-usage-info
/extension uninstall example.session-usage-info
```

## English

Package manifest.yaml and extension.py at the ZIP root, install through the existing `/extension install`
command, and send a normal message. The USER Handler reads committed primary user-input turns and a
separate context-window/per-turn-usage snapshot, logs them, and continues the original CORE receipt.
Other session-bound Handlers can use the same shared Ops; no manual notify call or platform Adapter change
is required. Context occupancy is an observation, not cumulative billing; retain measurement provenance.
Missing turn counters are None, while reported zero is retained. Core window/category defaults may also
be zero, so inspect measurement sources rather than assuming a provider observation. Existing category parts require breakdown=True. Queries do not
call a model, measure again, trigger a new event, or depend on message/append Ops. History reads run in a
thread, scan O(N) records, and fail explicitly on corruption, I/O errors, or a 10-second queue timeout.

At startup, the ops.build CORE creates a Builder and the session BUILTIN adds the methods. At runtime,
the native Adapter publishes a valid request-end usage summary with a host-bound SessionContext; Core
gives the USER Handler its Ops. Turns reads committed history; usage uses the bound host snapshot, or
committed history on another event without a host snapshot. Neither starts a query event. Finally,
next_handler continues the original usage chain and its existing CORE returns the acceptance receipt.
The notification is not per model/tool/token update. Reads are separate and do not share an atomic snapshot.

Usage returns schema_version, session_id, source, context and turn, or None if both observations are absent.
Host components are validated independently; an explicit empty host snapshot does not fall back to old history.
Breakdown is strictly bool; missing SessionContext raises RuntimeError. See [F_21](../../docs/features/F_21_session-usage-ops.md)
and [S_11](../../docs/specs/S_11_session-usage-ops.md) for registration, contracts, failure cases and acceptance.

## 字段填写速查 / Field guide

```python
turns = await ops.session.turns()                 # 不填会话、路径或初值
compact = await ops.session.usage()               # breakdown 默认 False
detailed = await ops.session.usage(breakdown=True) # 只保留已有 context.parts
```

用户唯一需要填写的查询参数是 bool breakdown。所有返回字段由平台提供，不需要回填到 Event；修改返回副本不会改变平台统计。重点区别：

| 返回内容 | 应如何理解 |
| --- | --- |
| turns 的 0 / None / 异常 | 0 是存在但无有效用户输入；None 是会话未知/已删除；读取异常不伪造 0 |
| usage 的 None | 两个有效观察均缺失；不是累计费用为 0 |
| usage.source | host 为本次绑定快照，history 为已提交历史；不同于下层统计来源标签 |
| context / turn | 分别为窗口占用和最近回合消耗，可来自不同请求/时点，不相加为账单 |
| context.timestamp / turn.timestamp | 前者生产路径为 ISO 8601 UTC 字符串；后者生产路径为 Unix 秒，旧 turn 时间原样保留须先判断 |
| occupancy_rate / percentage_of_window / percentage_of_input | 均为比例，0.2 表示 20%；不同于通知 usage_percent 的百分值 20.0 |
| estimated / authoritative_total | 类别估算与权威总量可以同时为 True；默认零值结合来源解释 |

全部字段（包含 measurement、parts 每项、通知输入与回执）的类型、缺省、合法取值、来源和具体示例见 F_21 第 3.2、4.1、4.2 节。English: callers only supply the boolean breakdown; all output fields are host-provided observations. Ratios use 0.2 for 20%, context and turn have different timestamp representations and may refer to different requests. Missing data is distinct from zero. This documentation revision does not change runtime behavior or repeat historical model acceptance.
