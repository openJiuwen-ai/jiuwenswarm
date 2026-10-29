# ops.session.messages 特性设计

| 项 | 值 |
| --- | --- |
| 编号 | F_23 / session-messages-ops |
| 归档日期 | 2026-10-29 |
| 文档来源 | 从 F_16 拆出历史查询说明；不新增或修改运行能力 |
| 关联计划 | [EXTV2-16 / Server PR-04](../designs/extensions-v2-development-plan.md#extv2-16-会话历史读写与追加前扩展) |
| 主责规约 | [S_06](../specs/S_06_session-history-ops.md) |

## 1. 范围、状态与依据

本方法已在 [PR !83](https://gitcode.com/huenrui_/jiuwenswarm/pull/83) 实现，随该 PR 评审和交付。本次从 F_16 拆成独立使用文档，业务代码、方法签名和交付计划不变。[F_16](F_16_session-history-ops.md) 继续说明 `session.append` 事件与 `ops.session.append` 方法；读取历史无需先追加消息或注册追加 Handler。

实现位于 Worker，依赖既有 Core EventBus、Registry、Ops Builder 和主会话历史服务；验证所用 Core 为 `ead700d7d18a247968d64de54918cf5d201a207a`。Front/Supervisor 沿用既有请求路由，查询本身不要求新增跨进程协议。本文只覆盖平台绑定的主会话，不提供任意 session_id、文件路径、Team 或子 Agent 查询参数。

## 2. 能力说明与 Claude 对照

`await ops.session.messages(limit=50, cursor=None)` 分页读取当前绑定主会话的持久化产品历史，适合审计、读取最近记录或构建自己的历史展示。调用者只填写本页条数和续页游标，得到按新到旧排列的记录以及续页信息。方法在线程中直接查询历史服务，不触发新的 EventBus 事件，不启动 Agent，也不自动更新前端。

对照官方 `anthropics/claude-code` 固定提交 `bf2323b3021c9d3db7a31e1e7d61068c3f704497` 的 `mods/types/claude-code.d.ts`（文件头 2.1.277）中 `$.session.messages` 的读取会话消息用途。本地方法采用 JSONL 产品记录和固定快照分页，返回下述四字段字典；不把 Claude 的消息对象、参数或返回容器当作本地 Schema，也不保证与其当前最新版互换兼容。

查询读取的是历史服务认可的可恢复记录，不是任意 JSONL 行、模型内存消息或仍在生成的片段。已有读取规则会跳过不可恢复记录、非法 JSON 行，并折叠用户正文中的 `<file-content>` 展开块；因此返回值不是文件的逐字节副本。内部提交标记 `_session_append` 不返回。

## 3. 事件与调用上下文

本特性没有独立查询事件、事件输入输出或查询 CORE，不注册 `session.messages` 事件。用户在一个**已经由平台绑定有效 SessionContext 的现有事件 Handler** 内调用共享 Ops 即可。示例选择 `context.usage.updated` 作为业务入口；它不是查询的必需事件，也不是查询自己产生的事件。

| 前提 | 由谁提供、应该填写什么 |
| --- | --- |
| SessionContext.session_id | 平台从真实业务目标收集，Core 冻结并绑定到本次 Ops；用户不通过查询参数选择另一个会话 |
| ops.session_context | Core 已有只读属性；不是异步查询方法，也不是用户自行收集/设置上下文的入口 |
| cwd / project_root / model | 本方法不读取，也不要求存在；只有有效会话身份仍可查询 |
| 包身份 | Registry/Core 绑定真实包身份；本查询不按包过滤记录，返回当前主会话的可恢复历史 |
| 原事件 Handler 链 | 用户查询后按原事件契约继续 `next_handler(event)`；查询结果不会自动成为原事件输出 |

平台负责触发业务事件、提供可信会话；用户不修改 Adapter，不调用 `notify_context_usage` 来索取上下文，不创建 EventBus 或 Builder。安装包只是发布订阅；相应业务事件尚未发生时，示例 Handler 不会自动执行。

## 4. 方法契约

### 4.1 签名、参数和填写规则

```python
page = await ops.session.messages(limit=20, cursor=None)
```

| 参数 | 类型 / 必填 / 默认值 | 含义、合法取值和应该填写什么 |
| --- | --- | --- |
| limit | int，可省略，默认 50 | 本页最多返回的可恢复记录数；只能 1～100，不接受 True/False、浮点数或数字字符串。例 `limit=20`；少于该数量不一定是错误 |
| cursor | str 或 None，可省略，默认 None | 首次查询或刷新最新历史填 None；续页原样传上次返回的非 None `next_cursor`。不填 snapshot_id，不解析/拼接游标，不跨会话复用；空串、全空白和非字符串非法 |

调用者不填写 session_id、路径、包 ID、消息内容、时间或事件类型；目标来自本次 Ops。`limit` 和 `cursor` 可以位置传参或按名称传参，推荐按名称；平台函数的首个 `ops` 参数由 Core 方法代理自动注入，用户不手工传入。

### 4.2 返回字典的全部字段

每次返回以下四个键，不以 None 代替整个正常查询结果。返回由平台构造，用户只读取，不向下一页回填记录内容。

| 返回字段 | 类型 / 是否必有 | 准确含义及怎样消费 |
| --- | --- | --- |
| messages | list[dict]，必有 | 当前页的主会话产品历史记录，按文件写入位置从新到旧；不是按 timestamp 重新排序，也不是 SessionAppendMessage 数组。空列表是合法结果 |
| next_cursor | str 或 None，必有 | 同一快照下一页的不透明游标；原样传给下一次 cursor。None 表示没有下一页，不要将 None 当成续页参数重新开始循环 |
| has_more | bool，必有 | 当前固定快照是否还有可恢复记录；为 True 时 next_cursor 有值。False 不表示查询之后不会出现新消息 |
| snapshot_id | str 或 None，必有 | 本次历史文件身份和截止位置对应的快照标识，用于关联页面；不是 cursor，也不是会话 ID。没有历史文件时为 None；文件存在但无可返回记录时可以有值 |

没有历史文件的正常返回：

```json
{"messages":[],"next_cursor":null,"has_more":false,"snapshot_id":null}
```

`messages` 内每条记录沿用产品历史格式。下面是当前正常生产者常用字段，不意味着为全部旧记录补齐键或规范化字段类型；消费旧记录和特有 extra 时使用 `.get(...)` 并检查类型。

| 记录字段 | 当前生产者的类型 / 可选性 | 含义、来源及使用方式 |
| --- | --- | --- |
| id | str，正常产品记录有 | 产品分组标识或扩展命名空间标识；同请求多条记录可能复用它，不能当作通用行唯一 ID |
| uuid | str，可缺省 | 新追加事件路径写入的行级身份，用于与事件/回执关联；旧或同步路径的记录可能没有 |
| role | str，正常产品记录有 | 产品兼容角色 user/assistant；旧 human 等值由原恢复规则处理。工具的完整角色可查 transcript_message.role |
| content | str，正常产品记录有 | 产品兼容显示正文；不是内容块数组，不表示原始工具 result 已删除，用户文件展开块可能被已有读取逻辑折叠 |
| request_id | str，正常产品记录有 | 产生该记录的平台请求标识，不是会话 ID，也不是查询参数 |
| channel_id | str，正常产品记录有 | 原记录所属接入渠道，如 web/extension；查询不能借此切换目标渠道 |
| timestamp | float，正常生产者填写 | Unix 时间戳，单位秒；不是毫秒或本地日期字符串。查询不将旧记录的其他类型统一转换 |
| event_type | str，可缺省 | 原产品事件类别，如 chat.final/chat.tool_result；不等于 EventBus 的事件名 |
| transcript_message | dict，可缺省 | 追加事件最终保存的公开消息快照；type/content/role/name/is_meta/tool_call_id/tool_calls 的含义见 [F_16 消息字段](F_16_session-history-ops.md#message-的全部字段)。旧记录可没有 |
| native_message | dict，可缺省 | 平台原生 BaseMessage 的 JSON 快照；基础字段为 role/content/name/metadata，原生子类可有工具调用、usage 等字段。不将它当作统一请求 Schema |
| tool_call_id / tool_calls / name | 依原生产者，可缺省 | 工具关联、助手工具调用和名称；查询只读保留，不修改关联 ID 或工具参数 |
| result / rendered_result | 依原生产者，可缺省 | 原始工具结果与呈现结果；结构可以不同。历史文本改写可能只更新呈现值，不能据此推断原始 result 已脱敏 |
| extension_message_id | str，仅扩展追加记录有 | 用户追加方法的稳定业务重试键；查询时不填写 |
| extension_package_id | str，仅扩展追加记录有 | 真实调用包 ID；平台直接调用且无包身份时为保留值 `<host>` |
| transcript_only | bool，仅扩展追加记录保证 True | 扩展留档标记；不表示已经触发新的 Agent 输入或回合 |

未列出的产品 extra 键、native_message.metadata 和供应商字段按原 JSON 保留，不开放额外写权限。返回记录是独立副本，修改字典或嵌套内容不会改写文件或影响另一 Handler。查询结果不自动缓存到 SessionContext，也不写入第二份会话状态。

### 4.3 固定快照、续页与刷新

首次 `cursor=None` 会排空已经进入历史写入队列的任务，再在文件锁内固定文件身份和截止位置。下一页继续同一截止位置；之后新增的行不进入旧游标。需要刷新最新记录时，用 `cursor=None` 开始新查询；不能用旧 snapshot_id 请求新快照。

同一有效游标和相同 limit 重试得到同一页；改变 limit 会改变本页最多返回的数量。每一页均按文件位置从新到旧；需要按正序展示时由调用者在收齐所需页面后自行整理。游标不承诺永久有效，文件替换或截断会使快照失效。

```python
page = await ops.session.messages(limit=20)
rows = list(page["messages"])
while page["has_more"]:
    page = await ops.session.messages(limit=20, cursor=page["next_cursor"])
    rows.extend(page["messages"])
# rows 为这个快照的查询结果；需要新消息时另起 cursor=None 的查询。
```

### 4.4 错误、取消和可观察效果

| 情况 | 方法行为与调用方处理 |
| --- | --- |
| 没有 SessionContext | RuntimeError；改在平台已绑定会话的入口使用，不能自己拼可信目标 |
| 绑定的会话 ID 非法/越界 | SessionHistoryError.code 为 INVALID_SESSION；不读取其他会话兜底 |
| limit 非法 | INVALID_MESSAGE_LIMIT；填写 1～100 的非 bool 整数 |
| cursor 类型/内容非法或属于其他会话 | INVALID_CURSOR；不解析或修补游标，重新开始合法查询 |
| 文件身份改变或被截断 | SNAPSHOT_CHANGED；放弃旧续页，按需求从 cursor=None 重新查询 |
| 文件读取等 I/O 失败 | HISTORY_IO_FAILED；与空历史区分，不用空列表伪造成功 |
| 显式启用旧 JSON 历史写入模式 | HISTORY_FORMAT_UNSUPPORTED；不承诺该模式支持本 JSONL 游标接口 |
| 会话没有历史文件 | 返回上面的正常空页，不抛 SESSION_NOT_FOUND，不创建空历史 |

查询使用 `asyncio.to_thread`，取消等待不强制停止已经开始的后台读取；它不因此追加记录或回滚其他写入，也没有独立超时参数。原服务可能在首次查询时排空队列或迁移旧历史文件格式；本能力不提供编辑/删除消息的操作。USER 未捕获的异常依外层 Core 的隔离规则处理，外层回合继续不代表本次查询成功。

## 5. 接入与兼容方案

### 5.1 平台装配和业务调用链

```text
启动：Worker 装配 → 既有 ops.build 事件
  → CORE 创建 Builder → session_ops_build BUILTIN 在 next 返回后登记 session.messages
  → 保存共享能力目录；Registry 发布用户 Handler

业务：已有平台业务方法收集有效 SessionContext
  → Worker bus.trigger(原事件, context)
  → Core 为 USER Handler 构造本次 Ops，并绑定会话/包身份
  → USER await ops.session.messages(limit, cursor)
  → Core 方法代理注入当前 Ops → session_messages
  → ops.session_context.session_id → asyncio.to_thread(read_session_messages, ...)
  → 参数校验 → 历史服务固定/校验快照 → 读取 JSONL 可恢复记录
  → 移除内部标记并复制记录 → 四字段字典直接返回 USER
  → USER 自行记录/展示 → next_handler(原事件) → 剩余原链 → 原事件 CORE
```

`ops.build` 的 CORE 只在装配阶段创建 Builder；BUILTIN 登记方法，不在这里查询历史。业务查询没有专用 USER/BUILTIN/CORE 新链，也不调用 `session.append`；原事件 CORE 继续负责原事件。EventBus 仍仅由 Worker 持有。

### 5.2 可单独使用的只读用户包

用户准备以下两个文件，打 ZIP 后通过现有扩展管理入口安装；该示例只订阅已有用量事件，不注册追加事件，不调用 append。

`manifest.yaml`：

```yaml
schemaVersion: 1
package:
  id: example.session-history-reader
  version: 1.0.0
  displayName: Session History Reader
extensions:
  - id: history-reader
    runtime: backend
    module: extension.py
```

`extension.py`：

```python
import logging

from jiuwenswarm.server.runtime.session.session_history_ops import SessionHistoryError

logger = logging.getLogger("jiuwenswarm.server.extensions.example.history_reader")

class HistoryReader:
    def register(self, registrar):
        registrar.register_event_handler("context.usage.updated", self.on_usage, {}, "user")

    async def on_usage(self, event, ops, next_handler):
        try:
            page = await ops.session.messages(limit=20)
            logger.info("history reader: session=%s count=%s has_more=%s",
                        ops.session_context.session_id, len(page["messages"]), page["has_more"])
            if page["has_more"]:
                older = await ops.session.messages(limit=20, cursor=page["next_cursor"])
                logger.info("history reader: older_count=%s", len(older["messages"]))
        except SessionHistoryError as error:
            logger.warning("history reader failed: code=%s", error.code)
        return await next_handler(event)

def create_extension():
    return HistoryReader()
```

正常完成一轮原生会话并产生有效用量通知后，可在平台日志观察 `history reader:`，它来自用户显式记录。其他事件只要具备可信会话也可调用该 Ops。需要前端展示时用户另接自己的消费/渲染链路；本方法不会自动 `ui.render`。卸载移除用户订阅，不删除历史文件、平台方法或原事件 CORE。

## 6. 验收与未决问题

2026-10-29 本次仅拆分和校核文档，未新增能力、未重跑业务测试或真实模型。参数、四个返回键、错误码、恢复过滤和完整用户示例均按当前源码核对；文档示例/链接/差异检查的结果由本次提交记录说明。

历史功能证据来自 PR04 的结构化消息版本（2026-10-29）：定向回归 388 项通过、1 项跳过，模型兼容回归 74 项通过。这是读写联合回归总数，不是本查询单项用例数，也不是本次文档拆分重跑的结果。命令及联合记录见 [F_16 第 6 节](F_16_session-history-ops.md#6-验收与未决问题)。

| 查询验收场景 | 预期和证明方式 | 已有证据入口 |
| --- | --- | --- |
| 有效会话但无工作区或历史 | 不依赖 cwd/model；无历史返回完整空页 | test_session_history_ops.py：test_read_requires_session_but_does_not_invent_missing_history；真实用户包无工作区读取 |
| 稳定分页及新消息 | 按新到旧读取；旧游标不夹入新行，新查询可见新行 | test_cursor_keeps_old_snapshot_and_new_query_sees_append；真实结果 fixedSnapshotPassed |
| 跨会话与替换 | 跨会话 INVALID_CURSOR；替换文件 SNAPSHOT_CHANGED | test_cursor_is_bound_to_session_and_replacement_invalidates_it |
| 参数校验 | 非法 limit/cursor 有明确错误，不伪造空页 | test_invalid_page_limit_is_explicit / test_invalid_cursor_is_explicit |
| 内容与返回隔离 | 内部标记不外露；记录与实际提交一致，返回副本互不修改 | 既有历史 Ops 与结构化消息回归；真实追加后读回记录 |
| Hook 中读取 | 查询不会另起追加链，不持提交锁执行 USER | test_append_policy_can_read_history_without_holding_commit_locks |
| 旧写入模式 | 既有写入仍可运行，游标查询明确拒绝 | test_explicit_legacy_writer_mode_keeps_its_write_but_refuses_cursor_query |

真实证据目录为 `D:\work_space\.codex-temp\server-pr04-message-contract-20261029\real-155735`，其中 result.json 的 fixedSnapshotPassed/rewrittenReadbackPassed/shippedExampleZipPassed 为 True，覆盖真实 Front → Supervisor → 子 Worker → Qwen3.8-Flash、安装用户包后查询及卸载。该记录同时包含追加验收，不将全部结果归给查询。

首版仍扫描历史文件；没有大历史性能、永久游标、原位同长度篡改检测或活跃模型消息一致性的承诺。未验证产品浏览器 UI、非 Windows 实机或远端 CI；当前主会话读取不等于已支持全部 Harness、Team/子 Agent 历史。

## 7. 参考资料

- [S_06 共享规约](../specs/S_06_session-history-ops.md)、[F_16 追加事件与方法](F_16_session-history-ops.md)。
- [Ops 登记与平台函数](../../handler_ops/session_ops.py)、[查询适配服务](../../../server/runtime/session/session_history_ops.py)、[底层快照读取](../../../server/runtime/session/session_history.py)、[可恢复记录规则](../../../server/wire_truncate.py)。
- [查询和写入联合回归](../../../../tests/unit_tests/extensions_v2/test_session_history_ops.py)、[结构化记录回归](../../../../tests/unit_tests/extensions_v2/test_session_history_messages.py)。
- [现成读写审计示例](../../examples/session_history_audit/README.md)、[共享 EventBus/Handler/Ops 接入说明](../designs/builtin-event-development-guide.md)。
- [Claude 固定提交定义](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)。
