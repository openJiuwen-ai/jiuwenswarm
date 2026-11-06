# session.detach 特性设计

| 项 | 值 |
| --- | --- |
| 编号 | F_24 / session-detach |
| 建档日期 | 2026-10-29 |
| 归档日期 | 2026-10-29 |
| 后续修订 | 2026-11-02，按既定 session.append 七段格式对齐 Claude Mods 字段；旧字段迁移见第 4.2 节，验证按版本分别记录；同日接续查询文档基线统一编号、验收标识和引用，契约不变 |
| 计划与进度 | [EXTV2-21 / Server PR11](../designs/extensions-v2-development-plan.md#extv2-21-界面与主会话关联解除通知) |
| 主责规约 | [S_12](../specs/S_12_session-detach.md) |

## 1. 范围、状态与依据

本项在平台移除一个具体客户端与旧会话的关联后，通知插件清理该关联资源。本次公开输入对齐 surface/clientId/reason，输出对齐 clientId；reason 使用 detach/end。切换细分来源及下一会话留在只读上下文；实际运行结束会清除仍在关联的客户端并发送 end 原因，然后进入 session.end。

对照 [Claude 官方类型声明](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)，固定提交 bf2323b3021c9d3db7a31e1e7d61068c3f704497，文件头 2.1.277。2026-11-02 重新下载官方 main 与固定文件，内容一致；这是此次字段核对的依据，不混用传统大写 SessionStart/SessionEnd Hooks。本地事件仍是 Python frozen dataclass，None 表示对应可选值缺失，并非直接接收任意 TypeScript/JSON 对象。

## 2. 能力说明与 Claude 对照

`session.detach` 告知插件“这个客户端已经离开旧会话”。USER 按旧关联身份取消自己的刷新、订阅或缓存，再 next；CORE 只确认旧关联确实已解除并回显 clientId。它本身不停止 Agent、不删除会话历史、不移除同一会话的其他客户端，也不负责执行第二次解除。

| 项 | Claude Mods 2.1.277 | 本次实现与使用影响 |
| --- | --- | --- |
| 输入 | surface / clientId / reason | 名称与含义对齐；旧本地身份放在只读 SessionContext |
| 原因 | detach / end | 显式解除或切换离开为 detach；运行结束仍关联的客户端为 end |
| 输出 | clientId | 不再使用 accepted/session_id/surface_id/attachment_id 包装 |
| 原业务请求 | Claude 有自己的客户端协议 | 本地保留 session.detach 请求的既有 session_id/view_id/attachment_id 及错误码；它不是公开事件输入的照传 |

## 3. 事件契约

### 3.1 触发前后与粒度

```text
来源一：新 session.create/session.switch 提交替换旧关联
来源二：Server session.detach 请求精确匹配旧 session/view/attachment
来源三：真实运行结束，平台收集该会话仍关联的所有客户端
  → 平台先移除/替换旧关联；保留旧会话 SessionContext 和旧关联版本
  → 构造 SessionDetachInput(surface, clientId, reason="detach" 或 "end")
  → 只读 context.session_attachment 保留 attachment_id、cause 和 next_session_id
  → await bus.trigger(Event("session.detach", input), 旧会话 context)
  → Core 为 USER 创建 Ops，绑定旧会话和旧关联资料
  → USER 按旧 attachment_id 清理自己的关联资源
  → await next_handler(event) → 剩余 Handler → session_detach_core
  → CORE 核对可信原输入、旧绑定和已经解除的关联，返回 clientId
  → USER 原样返回 → 平台通知函数
  → 切换：再通知仍有效的新 attach；显式解除：返回实际 detached
  → 运行结束：所有旧关联通知后继续 session.end 及原 Runtime 释放
```

每条旧关联的一次解除；重复显式解除在已无关联时返回 detached=False，不重发事件。切换先旧 detach、再新 attach；运行结束时先从表中移除该会话全部关联，再并发通知每个旧客户端，避免超时留下部分旧事实。稳定重连、页面隐藏、普通回答完成和空闲不推断为解除。

### 3.2 输入：每个字段表达什么、由谁填写

实际导入位置是 `jiuwenswarm.extensions_v2.event_types.session_detach`。以下构造器说明平台生成后的可观察形状；USER 无需自行构造或 trigger 此生命周期事件。输入字段全部只读，没有可改写字段。

```python
from jiuwenswarm.extensions_v2.event_types.session_detach import SessionDetachInput

SessionDetachInput(surface="desktop", clientId="web:view-main", reason="detach")
```

| 字段 | 类型 / 是否总有 | 准确含义 | 应该填写什么、示例 | Handler 权限 |
| --- | --- | --- | --- | --- |
| surface | RenderSurface，必填 | 该客户端离开前使用的渲染面类型，供插件选择自己的清理策略 | 从原 attach 的宿主事实回填 desktop 等枚举；不是旧窗口 ID 或下一会话的渲染面 | 只读 |
| clientId | str，必填，无省略默认 | 此次离开旧会话的客户端身份，用于日志或识别客户端 | 从原 attach 回填同一个值，如 web:view-main；一个客户端之后仍可加入 B，故迟到清理要结合旧 attachment_id | 只读；不换成新关联版本 |
| reason | str，必填，仅 detach/end | 为什么离开：detach 是明确离开，包括切换到其他会话；end 是其所在会话运行结束 | 平台按真实来源填写 detach 或 end。原 switch 不再是公开取值；需要细分切换时读取只读关联资料的 cause="switch" | 只读；不把断网/隐藏伪装为离开 |

#### 本次 Handler 的会话上下文与包身份

Event 输入与 `SessionContext` 是平台分别传入 `bus.trigger` 的两份资料。Core 给实际进入的 USER Handler 创建 Ops，绑定可信会话和包身份。`ops.session_context` 是已有只读属性，不采集资料、不组装上下文；`next_handler(event)` 继续当前事件链，不创建另一个事件。

| 入口 | 准确含义、来源与使用方式 |
| --- | --- |
| `ops.session_context.session_id` | 平台确定的本次查询会话身份。attach 绑定新会话，detach 绑定旧会话，end 绑定结束的会话；不能通过改 Event 切换查询目标 |
| `ops.session_context.cwd` | 仅有真实执行目录时才提供。start 来自原生 Adapter 已绑定目录；attach/detach/end 未确认时为 None，不使用 Worker 进程目录兜底 |
| `ops.session_context.metadata["project_root"]` | 平台已确认的项目根观察值；缺失时按相应查询返回 None。不是自动推导的 Git 根，也不为通知创建项目 |
| `ops.package_metadata.package_id` | Core 绑定的实际 USER 包身份，用于区分本包资源；不是当前会话身份，用户不自行填写 |

已有 cwd/root/model 等查询使用此可信入口。没有目录或模型资料时按各查询契约返回缺失值；不要求先发生用量事件，也不为展示初始化新模型。


平台另在 `ops.session_context.metadata["session_attachment"]` 提供只读的本地关联资料。它不属于 Claude 的公开 Event 字段，主要用于稳定识别本平台的某次关联：

| 字段 | 类型 / 何时提供 | 准确含义、取值和作用 |
| --- | --- | --- |
| `attachment_id` | str，attach/detach 均有 | 平台生成的 32 位小写十六进制 UUID，例如 `0123456789abcdef0123456789abcdef`。离开再进入会换新值，插件按它创建和清理资源，避免迟到清理误删新资源 |
| `view_id` | str，均有 | 原 Web 稳定窗口标识，如 `view-main`。它是原业务路由的窗口身份；不是 `surface` 类型、卡片 ID 或 WebSocket ID |
| `channel_id` | str，均有 | 原渠道，如 `web`。与 view_id 共同组成平台关联槽位；不能用它推断物理设备类型 |
| `mode` | str，均有 | 关联建立时确认的会话模式，如 `agent.work.normal`。仅观察，不作为模式切换命令 |
| `previous_session_id` | str 或 None，均有 | 该关联建立之前同一窗口的旧目标；首次进入为 None，不表示旧会话已经结束 |
| `cause` | str，仅 detach 有 | 本地解除来源：`switch` 切换替换、`detach` 显式解除、`end` 运行结束。对外 reason 中前两者统一为 detach，插件确需区分来源时读这里 |
| `next_session_id` | str 或 None，仅 detach 有 | 切到另一个已跟踪主会话时为新目标 ID；主动离开、运行结束或进入未跟踪范围时为 None。当前 Ops 仍绑定旧会话，读取新目标应等待下一条 attach |

USER 不修改这些资料。Server 显式解除请求仍使用原有 `session_id / view_id / attachment_id`，与 Event 的 `surface / clientId / reason` 分工不同。

### 3.3 输出：真实回执与结果消费

下面是默认 CORE 生成的输出形状，不是让 USER 自行返回假成功。合法 Handler 原样返回 `await next_handler(event)` 的真实结果；没有 deny 或 accepted 包装，也不能通过改回执改变平台事实。

```python
from jiuwenswarm.extensions_v2.event_types.session_detach import SessionDetachOutput

SessionDetachOutput(clientId="web:view-main")
```

| 字段 | 类型 / 返回时机 | 含义与调用方怎样使用 |
| --- | --- | --- |
| clientId | str，必填，无省略默认 | CORE 原样回显已离开的客户端。USER 返回真实 next 回执；宿主核对后继续原业务。它不证明全部外部清理成功，不恢复旧关联，也不替代 session.end 或删除结果 |

### 3.4 注册、筛选、处理示例

例如窗口 V 从 A 切到 B：旧 detach 绑定 A、clientId 仍为 web:V，插件按旧 attachment_id 取消 A 的刷新任务；随后新 attach 绑定 B，同一个 clientId 对应新的 attachment_id。迟到的 A 清理只能删旧键，不会误删 B 的资源。若只是关停 A 的运行，则对仍看 A 的窗口发送 reason=end，B 的窗口继续保留。

下列代码位于包内入口模块，由扩展管理入口按其 manifest 安装后发布 USER 订阅。空 matcher 匹配本次候选内的本事件；可按公开输入字段等值筛选，例如 surface/clientId/isInteractive/reason，大小写必须与具体事件的字段一致。未匹配不表示以后自动补发。

```python
"""Own resources by association identity, allowing independent view cleanup."""

import logging

logger = logging.getLogger("jiuwenswarm.server.extensions.example.session_detach")


class SessionDetachExample:
    """Keep only this package's per-association presentation resources."""

    def __init__(self):
        self._resources = {}

    def register(self, registrar):
        registrar.register_event_handler("session.attach", self.on_attach, {}, "user")
        registrar.register_event_handler("session.detach", self.on_detach, {}, "user")

    async def on_attach(self, event, ops, next_handler):
        receipt = await next_handler(event)
        identity = ops.session_context.metadata["session_attachment"]["attachment_id"]
        self._resources[identity] = {"session_id": ops.session_context.session_id, "client": receipt.clientId}
        return receipt

    async def on_detach(self, event, ops, next_handler):
        value = event.event_input
        identity = ops.session_context.metadata["session_attachment"]["attachment_id"]
        self._resources.pop(identity, None)
        receipt = await next_handler(event)
        logger.info("session detach example: session=%s surface=%s client=%s reason=%s",
                    ops.session_context.session_id, value.surface, receipt.clientId, value.reason)
        return receipt


def create_extension():
    return SessionDetachExample()
```

## 4. 方法契约

### 4.1 用户方法与平台入口

没有 `ops.session.detach()`。Server 现有业务请求仍为：

```python
params = {"session_id": "demo-session-a", "view_id": "view-main",
          "attachment_id": "0123456789abcdef0123456789abcdef"}
```

三个参数均必填、无省略默认：session_id 是当前旧会话；view_id 是原窗口标识；attachment_id 是仍有效的旧关联 UUID。channel_id 来自已路由请求，不允许 params 覆盖。关联身份从平台已有资料取得；USER Handler 可以从 ops.session_context.metadata["session_attachment"] 读取自己的旧关联事实。

成功 payload 沿用 session_id、surface_id（原 view_id）、attachment_id、detached。detached=True 表示实际移除了匹配项；False 表示无现存关联，不重复通知。过期版本为 STALE_ATTACHMENT，格式错误为 BAD_REQUEST，服务未就绪为 NOT_READY；这些业务响应字段不属于 SessionDetachOutput。

### 4.2 旧 Handler / 回执迁移

这些生命周期特性尚在 Server !99 中修订；旧示例应按下表迁移。平台内部的 snake_case 身份、稳定视图和校验状态继续保留，不因公开字段简化而削弱隔离。

| 旧用法 | 当前写法与影响 |
| --- | --- |
| input.surface_id | input.clientId 是客户端身份；原 view_id 从只读 session_attachment.view_id 读取 |
| input.session_id / attachment_id / channel_id / mode | 分别从 ops.session_context.session_id 和 metadata["session_attachment"] 读取 |
| input.reason == "switch" | input.reason == "detach"；确需细分来源时看 session_attachment.cause == "switch" |
| input.next_session_id | metadata["session_attachment"]["next_session_id"]，仍然不能改变当前 Ops 的旧会话绑定 |
| result.accepted / session_id / surface_id / attachment_id | 移除；原样返回 next 的 clientId 回执 |
| 运行结束不解除关联 | 当前同一宿主结束流程先 detach(reason="end")，再 session.end；不直接由 end CORE 删除关联 |

## 5. 接入与兼容方案

Worker 启动时注册事件类型、Matcher、Validator 和唯一默认 CORE；已有 `ops.build` 的 CORE 创建 Builder、BUILTIN 登记共享 Ops 方法，Registry 保存能力目录。业务发生时使用该目录创建绑定本次 SessionContext 的 Ops，不重跑 ops.build。这四条业务通知没有新增专门的 BUILTIN Handler。

USER 处理本包资源，通过 `return await next_handler(event)` 继续本条链。默认 CORE 只确认真实平台通知，不再次执行创建、切换、删除或关闭。输入均只读；提前短路伪造回执、修改输入、在 next 之后换身份无效，沿既有 Core USER 故障隔离处理。真实宿主操作的所有权和最终成功判断仍在原平台业务。

[attach_notifications.py](../../../server/runtime/session/attach_notifications.py) 保留平台关联表、精确移除和 release_for_end；[detach_notifications.py](../../../server/runtime/session/detach_notifications.py) 在可信作用域实际 trigger；[默认 CORE](../../event_handlers/session_detach.py) 核对旧会话和解除事实后只回显客户端。公开 reason 不携带本地 switch 扩展值，内部 cause 保留真实来源。

普通解除通知等待预算默认 2 秒；运行结束产生的 detach 与 session.end 共用宿主 1.5 秒结束预算。某个 USER 消耗预算时，后续通知可能没有执行机会；平台事实已解除，原 Runtime 继续释放，不伪造 CORE 回执。原调用取消传播，忽略取消的代码不能取得迟到有效回执；外部副作用不自动回滚。

字段范围以 [S_12](../specs/S_12_session-detach.md) 为准。Worker 持有唯一 EventBus；Front/Supervisor 继续沿用原请求路由。普通通知不新增聊天、历史写入或 UI 渲染。每份特性文档独立维护，不把 start、attach、detach、end 合成一个模糊的会话状态事件。

## 6. 验收与未决问题

### 2026-11-02 Claude Mods 字段修订

四项生命周期及字段定向用例共 **94 项通过**，包含在同一次最终回归的 **1365 项通过** 内，不重复累计。最终范围包括全部 extensions_v2、全部 Runtime、Worker 关闭、原生会话删除/归档和切换接线。5 项平台条件跳过：符号链接、命名管道及 3 项 Unix socket 用例。两个未修改的 LoopBeat 50ms 计时断言在本机存在约 47ms 读数；本分支与未修改基线分别保留复现，最终显式排除两项，不改阈值、不计为通过。

真实 Front → Supervisor → 子 Worker 验证：两个独立 Worker、3 轮真实模型对话，所有生命周期 USER Handler 在真实子进程中执行；没有 mock EventBus、Ops、模型或 Adapter，也没有介入 ui.render。5 次 detach（2 次 detach、3 次 end）均返回真实 clientId；显式解除、重复 detached=False、旧版本 STALE_ATTACHMENT、切换来源和结束前清理均通过；用户关联资源实际创建并删除。 两个 Worker PID 均已退出、端口关闭。

现成 HTML 的 A/B 教学案例也重新验证：A 在当前真实 start/usage CORE 下共享同一实例规则；B 的 SQLite HTTP 查询通过，但其模型发现/工具调用仍为教学宿主模拟，不把它当作上述真实模型工具使用证据。

证据目录：`D:/work_space/.codex-temp/align-session-mods-fields-20261102`，包括 final.log/xml/json、contracts.xml、focused-first.xml、baseline-liveness.xml、examples-verified.json。真实进程记录在 `D:/work_space/.codex-temp/align-session-mods-fields-20261102/real-150258`：result.json、USER JSONL、原生请求/响应、explicit-detach.json、两个 cleanup.json 和源码摘要。Core 使用 !110 的 0b5dd43f179be0a41ef2bd4bf0d43950bec9a92f；本次不新增 Core API。尚未执行远端 CI 或原生非 Windows 验收。

文档按 F_16 session.append 的七段结构维护；输入/输出对象、逐字段含义/填写/用途、USER 示例、平台入口、迁移与调用链分别展开。字段修订前的旧结果保留在以下历史区，不作为本次新契约的通过证据。

文档与本地 HTML 核对通过：四份特性保持 session.append 的同一七段结构，8 个输入/输出对象实际构造、28 段 Python 示例语法、120 个本地链接通过（跨仓链接按配套 Core 核对）。HTML 的生命周期调用链、字段表和实际函数已更新，A/B 完整案例逐字保留；桌面/手机视口无页面横向溢出，复制按钮及页面脚本通过。这是独立静态文档渲染检查，不计作产品浏览器 UI 验收。

本次重点覆盖：公开字段/回执及 matcher 大小写、可信 SessionContext 保持、同实例初始化去重、多窗口与切换、过期关联/输入改写/假回执拒绝、真实运行结束的 detach/end 顺序、超时/取消和进程清理。旧日期的验证不自动算成本次重新执行。

<details><summary>此前版本的验证记录：字段或触发范围以相应日期为准</summary>

本次 2026-10-31 仅按 F_16 的写法补齐输入/输出字段、注册示例、真实触发条件、Handler 职责和完整业务案例；运行代码与契约不变。文档核对包括 dataclass 字段与示例值、源码节选、Python/YAML/JSON 示例及相对链接。下列回归和真实服务结果均保留原运行日期与范围，不宣称本次重新执行业务或模型验收。

2026-10-31 合并交付验证：本特性与会话查询文档、session.start、session.end、session.attach/session.detach 一起整理到 [Server !99](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/99)，保留四个提交和各自特性文档。合并 PR 中的 session.start 依赖 [agent-core !110](https://gitcode.com/huenrui_/agent-core/merge_requests/110) 的公开 handler_ids；必须先合入 Core !110，再合入 Server。Core 文档因与目标分支重号改为 EV-07 / F_07，接口语义不变。

本次基于 Server `c6a07f909` 和合并最新目标分支的配套 Core，Windows / Python 3.11.15 下 Server 扩展、Runtime、历史、模型上下文与 Gateway 回归 **1744 项通过、3 项跳过**；Core Events/Extension/callback **587 项通过、1 项跳过**。Gateway 的 `test_web_disconnect_unregisters_physical_subscriptions` 在未修改目标分支和合并分支上均因 SERVICE_UNAVAILABLE 失败，最终合跑显式排除该 1 项，未将其计为通过。完整命令与 XML 在 `D:\work_space\.codex-temp\consolidate-server-prs-20261031`：server-final-command.json、server-final-regression.xml、core-command.json、core-regression.xml；基线对照见 gateway-baseline.xml 与 gateway-candidate.xml。本次未重跑真实模型或浏览器，以下旧日期的真实验证保持历史归属。

原 attach/detach 合并交付为 Server !95；本次保留按 channel/view/attachment 身份解除的契约及旧 detach → 新 attach 顺序，增加组合观察者的回归，不把永久删除、断线或隐藏自动解释为显式 detach。

2026-10-29，Windows / Python 3.11.15，Swarm 使用第 1 节固定基线和 PR !95 的 attach/detach 联合变更，Core 为 ead700d7d18a247968d64de54918cf5d201a207a。446 项扩展、原创建/切换、Runtime、生命周期、E2A 编解码、Gateway 代理及 Supervisor 转发回归通过，无失败、无跳过；一项外部 Authlib 弃用提示。此前首轮 48 项定向通过，已包含在回归范围中，不重复累计。未运行格式化或静态检查工具。

```powershell
python -m pytest -o addopts= -q tests/unit_tests/extensions_v2 tests/unit_tests/runtime/test_session_attach_provisioner.py tests/unit_tests/runtime/test_session_create_provisioner.py tests/unit_tests/runtime/test_session_switch_provisioner.py tests/unit_tests/runtime/test_session_provision_contract.py tests/unit_tests/runtime/test_session_lifecycle_participants.py tests/unit_tests/runtime/test_runtime_service.py tests/unit_tests/runtime/test_runtime_architecture.py tests/unit_tests/e2a/test_wire_codec.py tests/unit_tests/gateway/test_e2a_proxy.py tests/unit_tests/agentserver/test_supervisor_forwarding.py --asyncio-mode=auto -o log_cli=false
```

| 场景 | 操作与预期 | 已通过的证据 |
| --- | --- | --- |
| 两视图解除一个 | A 的两个视图只移除指定旧 attachment，另一个资源与关联保留 | 真实 USER 包、资源字典、显式请求及单测 |
| 真实切换提交 | prepare/abort 不通知；commit 后 detach 旧会话，再 attach 新目标；重复 commit 不重放 | test_session_attach_provisioner；真实 Front/Supervisor/子 Worker |
| 身份、重复与并发 | 旧 token 不删新关联；同视图重新进入生成新 ID；两个并发解除只一个 True | 真实 STALE_ATTACHMENT/BAD_REQUEST、并发 True/False 与单位测试 |
| 超时、取消和伪造 | 解除事实保留，迟到 CORE 无效，在途计数最终退出，所有输入不可改写 | 真实 Registry 用户包参数化与竞态测试 |
| 原会话不被结束 | 显式解除前后会话文件哈希相同；随后原会话可继续真实聊天 | 真实文件比较与一轮有效用量 |
| 归档与断线 | 归档不隐式解除；已有关联仍可显式清理；稳定 view_id 的重连不发 detach | 真实归档、清理与重连记录 |
| 卸载与来源 | 卸载 USER 后默认解除/切换继续；只用实际平台上下文和 Ops | 真实 ZIP 安装/卸载、源码哈希、子 Worker 进程与包身份 |

真实证据目录：`D:\work_space\.codex-temp\server-session-detach-event-20261029\real-202911`。最终成功运行记录 6 次 attach、5 次 detach；切换通知中旧 session/context/项目根正确，显式解除的旧关联清理不会删除新资源。没有任何用量时已经能通知和读取 root，未手工构造 SessionContext，未 mock 模型/Ops/Adapter。现成用户示例记录 5 次 accepted=True；另有 1 轮显式 Qwen3.8-Flash 业务回合及正用量证明解除不结束主会话。result.json、wire.jsonl、user-handler-results.jsonl、source-before/source-after 保存实际来源与前后一致性；cleanup.json 确认 Worker 退出、端口关闭。

实际接线发现原通用归档执行屏障会先拦截清理请求，现将 session.detach 作为仅清理已有匹配关联的请求交给自身身份核验，归档不会重新执行、恢复或写历史。验收客户端并发请求改用 UUID 避免时间粒度造成的请求编号复用；此前失败运行不作为通过证据，未因该测试客户端问题改动产品路由。

验证边界：真实链从 Front 开始，未验证产品浏览器视觉或真实 Gateway Web 连接；Gateway 此次只补已有 Web 转发表登记并运行代理回归。无非 Windows 实机、远端 CI 或跨重启恢复证据。用户的页面关闭自动请求接入、surfaces Ops、PR10 的结束联动仍未包含；通知异常不恢复已提交事实，旧资源清理不承诺外部副作用回滚。2 秒是每个通知的等待预算，一次切换含 detach 和 attach 两次通知，不代表整个切换只增加至多 2 秒。


2026-11-02 文档整理：以原 session.append 为参照，统一对象示例、五列通知输入表、三列输出表、会话资料说明及具体用户 Handler 案例；保留所有字段来源、默认／缺失、权限、超时和历史验证记录。只改文档，当前契约及 Core !110 前置不变。本轮核对结构、字段、示例与真实默认 CORE 回执、链接及差异，不重跑真实业务、模型或浏览器验收。

</details>

## 7. 参考资料

- [Claude Code Mods 固定类型声明](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)，SessionDetachInput/Result；参照小写 Mods 事件。
- [主责规约 S_12](../specs/S_12_session-detach.md)。
- [事件定义与校验](../../event_types/session_detach.py)、[默认 CORE](../../event_handlers/session_detach.py)。
- [session.append 文档格式基准](F_16_session-append.md)。
