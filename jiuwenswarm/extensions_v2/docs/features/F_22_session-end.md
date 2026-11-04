# session.end 特性设计

| 项 | 值 |
| --- | --- |
| 编号 | F_22 / session-end |
| 建档日期 | 2026-10-29 |
| 归档日期 | 2026-10-29 |
| 后续修订 | 2026-11-02，按既定 session.append 七段格式对齐 Claude Mods 字段；旧字段迁移见第 4.2 节，验证按版本分别记录；同日接续查询文档基线统一编号、验收标识和引用，契约不变 |
| 计划与进度 | [EXTV2-19 / Server PR10](../designs/extensions-v2-development-plan.md#extv2-19-主会话结束通知与用户资源清理) |
| 主责规约 | [S_10](../specs/S_10_session-end.md) |

## 1. 范围、状态与依据

本项保留“当前主会话运行结束时，给插件一次收尾机会”的语义，不恢复旧删除专用设计。本次 session_id 改为与 Claude 一致的公开 sessionId；reason/resume.id 保持，默认回执仅含 sessionId。平台的真实运行代次、去重和停止结果仍留在 Runtime，不加入公开字段。

对照 [Claude 官方类型声明](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)，固定提交 bf2323b3021c9d3db7a31e1e7d61068c3f704497，文件头 2.1.277。2026-11-02 重新下载官方 main 与固定文件，内容一致；这是此次字段核对的依据，不混用传统大写 SessionStart/SessionEnd Hooks。本地事件仍是 Python frozen dataclass，None 表示对应可选值缺失，并非直接接收任意 TypeScript/JSON 对象。

## 2. 能力说明与 Claude 对照

`session.end` 在平台真正停稳某个主会话运行后、释放该运行资源前通知 USER。插件可以保存自己按会话维护的摘要、关闭会话专用连接或任务，然后 next；CORE 核对可信结束通知并回显 sessionId，平台继续原释放。一次 query/模型回合完成不触发，结束运行也不要求删除历史。

| 项 | Claude Mods 2.1.277 | 本次实现与使用影响 |
| --- | --- | --- |
| 输入 | reason / sessionId / resume | 字段名及结构对齐，Python 公共属性同样为 sessionId |
| 原因 | clear / resume / logout / prompt_input_exit / other | 使用相同词表；当前原生资源清理和 Runtime 停机为 other，不伪造尚无业务的原因 |
| 输出 | sessionId | 仅回显实际结束身份，不返回 accepted/end_id |
| 结束与恢复 | 运行结束后通过持久 ID 恢复 | 主会话运行代次结束；历史可保留，同 ID 新运行结束可再次通知 |
| 关联收尾 | 会话结束时客户端 detach(reason=end) | Server 组合入口先解除该运行仍关联的客户端，再在同一结束预算内发 end |

## 3. 事件契约

### 3.1 触发前后与粒度

```text
平台真实入口 cleanup_session(A) / Runtime.close()
  → Coordinator 停稳目标真实代次执行，确认 CLOSED
  → 按 session_id + generation 合并同次通知尝试
  → 固定宿主 ExtensionSessionEndParticipant.session_run_ended
  → 构造只读 SessionContext(A) 和 SessionEndInput(reason, sessionId, resume)
  → Server 若仍有 A 的客户端关联：先移除全部旧关联，通知 detach(reason="end")
  → bind_end_invocation → await bus.trigger(Event("session.end", input), context_A)
  → Core 为 USER 创建 Ops，绑定结束会话与当前包
  → USER 保存自己的摘要、释放自己的 A 专用资源
  → await next_handler(event) → 剩余 Handler → session_end_core
  → CORE 核对有效宿主调用、原输入与绑定会话
  → 返回 SessionEndOutput(sessionId=A)
  → USER 原样返回 → 平台通知任务 → Runtime 继续原内存资源释放
  → 整个 Runtime 关闭时，结束通知之后才卸载扩展
```

一个已登记且停稳的 Runtime 主会话代次最多尝试一次。重复 cleanup 或随后 close 不重放；同一持久 ID 恢复后会登记新 generation，新运行结束可再次通知。普通回复完成、取消当前生成、idle、单纯界面切换/关联变化和传输重连不触发。删除已登记运行也会结束它；仅有离线存档时不虚构运行结束。

### 3.2 输入：每个字段表达什么、由谁填写

实际导入位置是 `jiuwenswarm.extensions_v2.event_types.session_end`。以下构造器说明平台生成后的可观察形状；USER 无需自行构造或 trigger 此生命周期事件。输入字段全部只读，没有可改写字段。

```python
from jiuwenswarm.extensions_v2.event_types.session_end import SessionEndInput, SessionResume

SessionEndInput(reason="other", sessionId="demo-session-a",
                resume=SessionResume(id="demo-session-a"))
```

| 字段 | 类型 / 是否总有 | 准确含义 | 应该填写什么、示例 | Handler 权限 |
| --- | --- | --- | --- | --- |
| reason | str，必填，无省略默认 | 为什么结束本次运行，用于日志、matcher 和用户收尾策略；不表示是否永久删除 | 宿主按真实业务填写 clear / resume / logout / prompt_input_exit / other。当前资源释放与正常停机为 other，不填 session_deleted | 只读；不能据此取消结束 |
| sessionId | str，必填，无省略默认 | 本次结束的主会话身份，用于定位本包按会话维护的资源 | Runtime 已登记且已停稳的真实会话 ID，如 demo-session-a；须与 ops.session_context.session_id 一致，不填窗口 ID 或包 ID | 只读；Python 也使用这一大小写 |
| resume | SessionResume，必填 | 以后可用于恢复该持久会话身份的资料对象；不携带历史正文或授权 | 平台填写 SessionResume(id="demo-session-a")；USER 使用 value.resume.id 属性，不把它当 dict 索引 | 对象及嵌套字段只读 |
| resume.id | str，嵌套必填 | 原会话的恢复 ID。可保存到用户摘要，之后交由平台原恢复入口使用 | 等于本次 sessionId，如 demo-session-a。没有写过历史、关闭持久化或存档后来被删除时，不能保证可恢复 | 只读；不是一次性访问令牌 |

#### 本次 Handler 的会话上下文与包身份

Event 输入与 `SessionContext` 是平台分别传入 `bus.trigger` 的两份资料。Core 给实际进入的 USER Handler 创建 Ops，绑定可信会话和包身份。`ops.session_context` 是已有只读属性，不采集资料、不组装上下文；`next_handler(event)` 继续当前事件链，不创建另一个事件。

| 入口 | 准确含义、来源与使用方式 |
| --- | --- |
| `ops.session_context.session_id` | 平台确定的本次查询会话身份。attach 绑定新会话，detach 绑定旧会话，end 绑定结束的会话；不能通过改 Event 切换查询目标 |
| `ops.session_context.cwd` | 仅有真实执行目录时才提供。start 来自原生 Adapter 已绑定目录；attach/detach/end 未确认时为 None，不使用 Worker 进程目录兜底 |
| `ops.session_context.metadata["project_root"]` | 平台已确认的项目根观察值；缺失时按相应查询返回 None。不是自动推导的 Git 根，也不为通知创建项目 |
| `ops.package_metadata.package_id` | Core 绑定的实际 USER 包身份，用于区分本包资源；不是当前会话身份，用户不自行填写 |

已有 cwd/root/model 等查询使用此可信入口。没有目录或模型资料时按各查询契约返回缺失值；不要求先发生用量事件，也不为展示初始化新模型。

### 3.3 输出：真实回执与结果消费

下面是默认 CORE 生成的输出形状，不是让 USER 自行返回假成功。合法 Handler 原样返回 `await next_handler(event)` 的真实结果；没有 deny 或 accepted 包装，也不能通过改回执改变平台事实。

```python
from jiuwenswarm.extensions_v2.event_types.session_end import SessionEndOutput

SessionEndOutput(sessionId="demo-session-a")
```

| 字段 | 类型 / 返回时机 | 含义与调用方怎样使用 |
| --- | --- | --- |
| sessionId | str，必填，无省略默认 | 默认 CORE 原样回显实际结束会话。USER 返回 next 的真实结果，宿主核对后继续原释放。它不证明进程已经退出、外部资源全部释放或历史删除成功 |

### 3.4 注册、筛选、处理示例

例如插件在 A 的每次用量更新后保存活跃摘要。平台真正清理 A 的运行或正常关闭 Worker 时，on_end 按 sessionId 清理 A 的活跃缓存，记录 reason 与 resume.id；B 的缓存不受影响。示例包记录真实清理日志；若需要跨进程持久保存摘要，应使用用户自己的存储能力，不能把普通内存字典当作已落盘。

下列代码来自 [现有示例包](../../examples/session_end/extension.py)，由扩展管理入口按其 manifest 安装后发布 USER 订阅。空 matcher 匹配本次候选内的本事件；可按公开输入字段等值筛选，例如 surface/clientId/isInteractive/reason，大小写必须与具体事件的字段一致。未匹配不表示以后自动补发。

```python
"""Keep user-owned session state and release it when that Runtime run ends."""

import logging

logger = logging.getLogger("jiuwenswarm.server.extensions.example.session_end")


class SessionEndExample:
    """A user's Session resources, independent of platform history ownership."""

    def __init__(self):
        self.cache = {}

    def register(self, registrar):
        registrar.register_event_handler("context.usage.updated", self.on_usage, {}, "user")
        registrar.register_event_handler("session.end", self.on_end, {}, "user")

    async def on_usage(self, event, ops, next_handler):
        self.cache[ops.session_context.session_id] = {"project_root": await ops.session.root()}
        return await next_handler(event)

    async def on_end(self, event, ops, next_handler):
        session_id = ops.session_context.session_id
        released = self.cache.pop(session_id, None) is not None
        logger.info("session end example: session=%s reason=%s resume=%s released=%s",
                    session_id, event.event_input.reason, event.event_input.resume.id, released)
        return await next_handler(event)


def create_extension():
    return SessionEndExample()
```

## 4. 方法契约

### 4.1 用户方法与平台入口

没有新增 `ops.session.end()`。宿主已有 Python 入口为：

```python
await runtime.cleanup_session(channel_id="web", session_id="demo-session-a")
await runtime.close()
```

这些平台方法仍采用 Python session_id 参数，不是 Event 的公开 sessionId。end_reason 可由宿主传入 SessionRunEndReason 五值之一，默认 OTHER；无效原因在资源变化前拒绝。cleanup_session 返回原 Manager 清理 bool，close 返回 None；不拿事件回执冒充最终清理成功。reset_plan_state 沿用原 API 与删除事务补偿规则。

### 4.2 旧 Handler / 回执迁移

这些生命周期特性尚在 Server !99 中修订；旧示例应按下表迁移。平台内部的 snake_case 身份、稳定视图和校验状态继续保留，不因公开字段简化而削弱隔离。

| 旧用法 | 当前写法与影响 |
| --- | --- |
| event.event_input.session_id | event.event_input.sessionId；matcher 改为 sessionId |
| result.session_id | result.sessionId；原样保留 await next_handler(event) 的真实回执 |
| SessionContext.session_id / runtime.cleanup_session(session_id=...) | 平台上下文和 Python 方法参数仍保持 snake_case，不做全仓文本替换 |
| accepted / end_id / reason=session_deleted | 均不属于当前契约；按 reason/sessionId/resume 处理 |
| 把本次回执当作历史删除或 Worker 退出结果 | 只确认结束通知；原平台继续释放，历史可保留 |

## 5. 接入与兼容方案

Worker 启动时注册事件类型、Matcher、Validator 和唯一默认 CORE；已有 `ops.build` 的 CORE 创建 Builder、BUILTIN 登记共享 Ops 方法，Registry 保存能力目录。业务发生时使用该目录创建绑定本次 SessionContext 的 Ops，不重跑 ops.build。这四条业务通知没有新增专门的 BUILTIN Handler。

USER 处理本包资源，通过 `return await next_handler(event)` 继续本条链。默认 CORE 只确认真实平台通知，不再次执行创建、切换、删除或关闭。输入均只读；提前短路伪造回执、修改输入、在 next 之后换身份无效，沿既有 Core USER 故障隔离处理。真实宿主操作的所有权和最终成功判断仍在原平台业务。

[Runtime](../../../runtime/service.py) 持有停稳、代次去重和实际释放；[平台通知模块](../../../server/runtime/session/end_notifications.py) 构造上下文、设置预算、在途计数和实际 trigger；[默认 CORE](../../event_handlers/session_end.py) 校验原输入和绑定身份，仅返回 sessionId。Server 组合入口把关联清理回调接入同一结束任务；独立 Runtime 没有关联表时无需创建一个。

宿主结束任务默认总预算 1.5 秒，包括关联收尾与 end 通知。失败/超时记日志后继续原释放，不允许 USER 拒绝或替换结果；原关闭取消仍按实际取消传播。已尝试不自动重放，忽略取消的任务可能晚退出，在途计数直到实际退出才释放。崩溃、强杀或执行未能停稳时不保证正常结束 Hook；不承诺撤销已提交外部操作。

字段范围以 [S_10](../specs/S_10_session-end.md) 为准。Worker 持有唯一 EventBus；Front/Supervisor 继续沿用原请求路由。普通通知不新增聊天、历史写入或 UI 渲染。每份特性文档独立维护，不把 start、attach、detach、end 合成一个模糊的会话状态事件。

## 6. 验收与未决问题

### 2026-11-02 Claude Mods 字段修订

四项生命周期及字段定向用例共 **94 项通过**，包含在同一次最终回归的 **1365 项通过** 内，不重复累计。最终范围包括全部 extensions_v2、全部 Runtime、Worker 关闭、原生会话删除/归档和切换接线。5 项平台条件跳过：符号链接、命名管道及 3 项 Unix socket 用例。两个未修改的 LoopBeat 50ms 计时断言在本机存在约 47ms 读数；本分支与未修改基线分别保留复现，最终显式排除两项，不改阈值、不计为通过。

真实 Front → Supervisor → 子 Worker 验证：两个独立 Worker、3 轮真实模型对话，所有生命周期 USER Handler 在真实子进程中执行；没有 mock EventBus、Ops、模型或 Adapter，也没有介入 ui.render。3 次 end 均使用 reason/sessionId/resume，CORE 回显 sessionId；同 ID 在新 Worker 恢复后再次结束，先完成对应关联的 detach(end)，会话运行资源实际清理，历史逐字节保留并可续聊。 两个 Worker PID 均已退出、端口关闭。

现成 HTML 的 A/B 教学案例也重新验证：A 在当前真实 start/usage CORE 下共享同一实例规则；B 的 SQLite HTTP 查询通过，但其模型发现/工具调用仍为教学宿主模拟，不把它当作上述真实模型工具使用证据。

证据目录：`D:/work_space/.codex-temp/align-session-mods-fields-20261102`，包括 final.log/xml/json、contracts.xml、focused-first.xml、baseline-liveness.xml、examples-verified.json。真实进程记录在 `D:/work_space/.codex-temp/align-session-mods-fields-20261102/real-150258`：result.json、USER JSONL、原生请求/响应、explicit-detach.json、两个 cleanup.json 和源码摘要。Core 使用 !110 的 0b5dd43f179be0a41ef2bd4bf0d43950bec9a92f；本次不新增 Core API。尚未执行远端 CI 或原生非 Windows 验收。

文档按 F_16 session.append 的七段结构维护；输入/输出对象、逐字段含义/填写/用途、USER 示例、平台入口、迁移与调用链分别展开。字段修订前的旧结果保留在以下历史区，不作为本次新契约的通过证据。

文档与本地 HTML 核对通过：四份特性保持 session.append 的同一七段结构，8 个输入/输出对象实际构造、28 段 Python 示例语法、120 个本地链接通过（跨仓链接按配套 Core 核对）。HTML 的生命周期调用链、字段表和实际函数已更新，A/B 完整案例逐字保留；桌面/手机视口无页面横向溢出，复制按钮及页面脚本通过。这是独立静态文档渲染检查，不计作产品浏览器 UI 验收。

本次重点覆盖：公开字段/回执及 matcher 大小写、可信 SessionContext 保持、同实例初始化去重、多窗口与切换、过期关联/输入改写/假回执拒绝、真实运行结束的 detach/end 顺序、超时/取消和进程清理。旧日期的验证不自动算成本次重新执行。

<details><summary>此前版本的验证记录：字段或触发范围以相应日期为准</summary>

### 2026-11-02 运行结束版本

定向验证：29 项通过，无跳过。覆盖安装的真实 USER 模块、实际 EventBus/CORE/Ops、五种宿主原因、旧字段迁移、身份/恢复资料保护、同代次去重、同 ID 新代次再次结束、回合完成不触发、执行停稳顺序、保留记录文件、新代次等待旧清理、Worker 停机先通知后卸载及多会话并发通知。Runtime 资源管理器在单元场景中是测试替身，不把该结果描述成真实模型或完整产品存档恢复验收。

2026-11-02 最终回归：1362 项通过、2 项平台条件跳过；上述 29 项定向覆盖包含在回归内，不重复累计。范围包含全部 extensions_v2、全部 Runtime、Worker 关闭、原生会话删除/归档和会话切换回归。另有两个未修改的 LoopBeat 50ms 计时用例，在本机原始提交 ce48416582c998ab7301a952ffeb7f86f7f5ef01 的独立工作树同样读到约 47ms；已保留原始失败和基线复现，最终回归显式排除这两项，不修改它们的阈值或业务实现。平台跳过原因见 final.xml。

真实联调：最终源码在两个独立 Front → Supervisor → Worker 生命周期中完成 3 轮 Qwen3.8-Flash 模型对话。第一轮运行持有 A/B，普通回合、界面切换和空闲连接断开不提前发 end；正常停机时两个 USER Hook 都释放了自己实际创建的资源，CORE 回显身份，两个 JSONL 历史文件逐字节保持不变。第二个 Worker 通过原生 session.switch 恢复同一 A ID，继续真实模型对话，旧历史前缀保留；正常停机再次收到 A 的运行结束通知。两次 Worker 内 start 都仅执行一次。所有 Hook 在独立子 Worker PID 中运行；没有 mock Ops、模型或 Adapter，没有手工构造 SessionContext，也没有介入 ui.render。

最终真实证据：D:/work_space/.codex-temp/session-end-run-lifecycle-20261102/real-103525。result.json、initial-result.json、resumed-result.json、原生请求/回复、USER JSONL、源码摘要和两个 cleanup.json 保留证据；两个 Worker 均已退出、端口关闭。real-final-exit.json 确认验证进程退出码为 0。

文档验证：4 段 Python 说明语法、2 个真实输入/输出构造器、文档 USER 案例与实际默认 CORE、60 个本地文档链接通过（其中 4 个跨仓链接按配套 Core 工作树核对）。本地完整调用链 HTML 已同步案例、输入输出字段表和 8 个实际函数；内部锚点/复制按钮有效，start 函数区未变。没有运行格式化或静态检查工具。

当前证据统一保存在 D:/work_space/.codex-temp/session-end-run-lifecycle-20261102：final.log/xml/json、baseline-liveness.log/xml/json、docs-verified.json、verification.json。Core 使用 !110 的 0b5dd43f179be0a41ef2bd4bf0d43950bec9a92f；本项无需新增 Core API，组合 PR !99 仍因 start 保持 Core !110 在先的前置。未执行浏览器 UI、非 Windows 实机或远端 CI；本次不将这些范围写成通过。

### 历史记录：2026-10-29 删除专用版本及之后的文档整理

以下原记录保留追溯。其触发范围、2 秒预算和旧字段均属于旧契约，已经由本次运行结束版本替代，不能用作本次新增入口或格式的通过证据。

<details><summary>展开历史验收记录</summary>

本次 2026-10-31 仅按 F_16 的写法补齐输入/输出字段、注册示例、真实触发条件、Handler 职责和完整业务案例；运行代码与契约不变。文档核对包括 dataclass 字段与示例值、源码节选、Python/YAML/JSON 示例及相对链接。下列回归和真实服务结果均保留原运行日期与范围，不宣称本次重新执行业务或模型验收。

2026-10-31 合并交付验证：本特性与会话查询文档、session.start、session.end、session.attach/session.detach 一起整理到 [Server !99](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/99)，保留四个提交和各自特性文档。合并 PR 中的 session.start 依赖 [agent-core !110](https://gitcode.com/huenrui_/agent-core/merge_requests/110) 的公开 handler_ids；必须先合入 Core !110，再合入 Server。Core 文档因与目标分支重号改为 EV-07 / F_07，接口语义不变。

本次基于 Server `c6a07f909` 和合并最新目标分支的配套 Core，Windows / Python 3.11.15 下 Server 扩展、Runtime、历史、模型上下文与 Gateway 回归 **1744 项通过、3 项跳过**；Core Events/Extension/callback **587 项通过、1 项跳过**。Gateway 的 `test_web_disconnect_unregisters_physical_subscriptions` 在未修改目标分支和合并分支上均因 SERVICE_UNAVAILABLE 失败，最终合跑显式排除该 1 项，未将其计为通过。完整命令与 XML 在 `D:\work_space\.codex-temp\consolidate-server-prs-20261031`：server-final-command.json、server-final-regression.xml、core-command.json、core-regression.xml；基线对照见 gateway-baseline.xml 与 gateway-candidate.xml。本次未重跑真实模型或浏览器，以下旧日期的真实验证保持历史归属。

原独立交付为 Server !93；合并后同一个 RuntimeParticipantRegistry 同时保留固定删除观察者和固定 foreground 观察者。KVC 启停、挂起与 lease 释放的用例同时核对两者不会互相覆盖；本事件自身不要求 handler_ids，也不因此新增 end/detach 联动。

2026-10-29，Windows / Python 3.11.15，使用上文固定的 Server 基线与 Core ead700d7d18a247968d64de54918cf5d201a207a，当前工作区变更包含本特性。404 项扩展/Runtime/删除与归档回归通过，无跳过；另单独重验本特性的 19 项用例通过（包含在该回归范围中，不重复累计）。仅出现外部 Authlib 弃用提示，未运行格式化或静态检查工具。

```powershell
python -m pytest -o addopts= -q tests/unit_tests/extensions_v2 tests/unit_tests/runtime/test_runtime_session_provisioner.py tests/unit_tests/runtime/test_runtime_service.py tests/unit_tests/runtime/test_session_lifecycle_participants.py tests/unit_tests/runtime/test_runtime_architecture.py tests/unit_tests/server/test_archive_lifecycle.py tests/unit_tests/server/test_agent_lifecycle_request.py --asyncio-mode=auto -o log_cli=false
python -m pytest -o addopts= -q tests/unit_tests/extensions_v2/test_session_end.py --asyncio-mode=auto -o log_cli=false
```

| 场景 | 操作与预期 | 实际证据 |
| --- | --- | --- |
| 真用户包与默认 CORE | 安装用户模块，目标/包身份/Ops 正确，next 得到真实回执 | test_session_end.py；真实服务的两个安装包均执行 |
| 重复/并发与失败重试 | 同一未完成删除至多尝试一次，提交后释放状态 | 定向用例与真实 concurrent-delete/repeated-delete 记录 |
| 防改写/短路 | 五个输入字段、提前伪造结果、后置改写及 bool/int 替换无效，CORE 仍处理原目标 | 注册包参数化用例，检查真实调用作用域中的 CORE 回执 |
| 超时/取消 | 合作取消、忽略取消的任务、原请求取消均有明确边界 | 2 秒生产预算的短预算定向构造；超时后不允许迟到 CORE，包计数最终归零 |
| 原生删除顺序 | USER/通知 CORE 完成时目录仍在，随后原 Runtime 实际删除 | 实际 AgentRuntime.delete_session 测试；真实 Worker 读回同样事实 |
| 不误判结束 | 普通 cleanup、断连/重连、归档/恢复不通知 | Runtime 定向测试；真实连接与归档操作，通知计数保持不变 |
| 参与者共存 | KVC 启停、suspend 和 lease release 保留宿主参与者 | 实际 KVCacheApplicationOwner 与 Registry 回归 |
| 作用域与卸载 | 两会话互不串用，Team 排除；卸载后原删除继续 | 定向 Team/跨会话用例；真实卸载后创建并删除第三个会话 |

真实服务证据：`D:\work_space\.codex-temp\server-session-end-event-20261029\real-191323`。实际 Front → Supervisor → 子 Worker → Qwen3.8-Flash 完成两个模型回合；用户包在已有用量事件创建自己拥有的资源文件，在 session.end 内删除它们并取得默认 CORE 回执。断连、归档/恢复不产生结束通知；两个目标均只通知一次，平台目录随后实际删除。现成示例日志出现两次 released=True。未 mock Ops、模型或 Adapter，未手工构造 SessionContext。

result.json、user-handler-results.jsonl、各删除/归档响应、日志和 source-before/source-after 收据记录来源及一致性；cleanup.json 确认 Worker 已退出、端口关闭。首次联调脚本把归档请求误写为单个 session_id，收到 BAD_REQUEST 后已按既有 session_ids 协议修正；该次不计为验收通过，当前结论以上述成功运行记录为准。

已知边界：没有跨进程崩溃/重启 exactly-once；用户资源在取消或平台随后失败时可能已清理，平台不恢复外部副作用。没有真实服务下的恶意无限任务终止保证、非 Windows 实机、产品浏览器 UI、远端 CI 或大并发性能证据。不同 Harness 的统一结束语义、进程退出和恢复资料仍不在首版范围。

2026-11-02 文档整理：以原 session.append 为参照，统一对象示例、五列通知输入表、三列输出表、会话资料说明及具体用户 Handler 案例；保留所有字段来源、默认／缺失、权限、超时和历史验证记录。只改文档，当前契约及 Core !110 前置不变。本轮核对结构、字段、示例与真实默认 CORE 回执、链接及差异，不重跑真实业务、模型或浏览器验收。

</details>

</details>

## 7. 参考资料

- [Claude Code Mods 固定类型声明](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)，SessionEndInput/Result；参照小写 Mods 事件。
- [主责规约 S_10](../specs/S_10_session-end.md)。
- [事件定义与校验](../../event_types/session_end.py)、[默认 CORE](../../event_handlers/session_end.py)。
- [示例包说明](../../examples/session_end/README.md)。
- [session.append 文档格式基准](F_16_session-append.md)。
