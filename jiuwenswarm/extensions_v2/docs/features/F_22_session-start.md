# session.start 特性设计

| 项 | 值 |
| --- | --- |
| 编号 | F_22 / session-start |
| 建档日期 | 2026-10-28 |
| 后续修订 | 2026-11-03，包定向从 Core `handler_ids` 改为宿主注册期匹配；2026-11-02 按既定 session.append 七段格式对齐 Claude Mods 字段，旧字段迁移见第 4.2 节 |
| 计划与进度入口 | [EXTV2-18 / Server PR06](../designs/extensions-v2-development-plan.md#extv2-18-激活实例首次输入前初始化) |
| 主责契约 | [S_12](../specs/S_12_session-start.md) |

## 1. 范围、状态与依据

本项保留每个 Worker 包激活实例在首次受支持原生输入前至多尝试一次初始化通知的粒度。公开观察字段和默认回执保持 Claude Mods 对齐版本；包身份、真实激活记录、generation、发布的 Handler IDs 和请求路由仍由平台持有，不放进公开 Event。宿主注册期绑定可信包匹配条件，Core 恢复普通 `trigger(event, session)` 和既有 matcher；不改 session.append。

对照 [Claude 官方类型声明](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)，固定提交 bf2323b3021c9d3db7a31e1e7d61068c3f704497，文件头 2.1.277。2026-11-02 重新下载官方 main 与固定文件，内容一致；这是此次字段核对的依据，不混用传统大写 SessionStart/SessionEnd Hooks。本地事件仍是 Python frozen dataclass，None 表示对应可选值缺失，并非直接接收任意 TypeScript/JSON 对象。

## 2. 能力说明与 Claude 对照

`session.start` 在原生 chat.send 完成权限准入、模型应用及执行目录绑定后、Agent 处理该输入前触发。USER 收到 cwd、surface、isInteractive，可以准备自己的规则、缓存或能力，然后 next；CORE 回显可信 cwd，由初始化门消费后继续原输入。后续 Handler 是否复用资源由插件自己的同一实例状态决定，事件本身不会自动把规则传给模型。

| 项 | Claude Mods 2.1.277 | 本次实现与使用影响 |
| --- | --- | --- |
| 输入与输出 | cwd / surface / isInteractive；输出 cwd | 字段名及默认回执对齐，Python 属性也使用 isInteractive |
| 粒度 | 每个已加载插件在首次提示前一次，新加载再初始化 | Worker 包激活实例一次；切换会话、每轮完成不会重新 start |
| 渲染事实 | RenderSurface 或 null | 原生 Web 宿主声明 desktop；未知为 None，不声称已经 attach |
| 身份 | 通过引擎会话/插件入口读取 | 会话和包分别通过 ops.session_context / ops.package_metadata；代次留在 Gate |

`surface` 与 `clientId` 是两个维度：前者是渲染面类型，后者是具体客户端。当前 Web 宿主明确使用 `desktop` 渲染面协议，不通过浏览器 UA 猜设备；手机浏览器不会仅因屏幕小就自动变成 mobile。其他宿主只有在明确声明 terminal/mobile/vscode 等类型后才可报告。start 不知道渲染面时为 None；attach 的 surface 必须已知，否则宿主不应伪造一次成功客户端关联。

当前关联参与者的默认渠道声明为 `web → desktop`，其他渠道默认不发 attach。clientId 使用 `channel_id:view_id`，例如 `web:view-main`，避免不同渠道的同名窗口冲突；不是每次重新生成的 attachment_id。旧调用缺少 view_id 时保留原平台降级标识，多个窗口应各自提供稳定且不同的 view_id。

## 3. 事件契约

### 3.1 触发前后与粒度

```text
安装/加载：Runtime 发布 Handler，宿主 EventBus 从可信包元数据注入私有包匹配条件
原生 chat.send unary / stream → Front / Supervisor → Worker
  → 原权限准入、模型应用、执行目录绑定
  → Adapter 构造真实 SessionContext（session_id、cwd、项目根/模型等可得资料）
  → manager.ensure_session_started(..., surface=宿主声明, is_interactive=原能力)
  → SessionStartGate 选择尚未尝试的真实激活实例，以发布 ID 核对实例版本
  → 构造 SessionStartInput(cwd, surface, isInteractive)，私有绑定真实 session_id 与目标包 ID
  → await bus.trigger(Event("session.start", input), context)
  → session.start matcher 先筛目标包，再检查用户声明的公开字段 pattern
  → Core 为 USER 创建本次 Ops，绑定会话和包身份
  → USER 准备本包资源 → await next_handler(event)
  → 剩余 Handler → session_start_core 核对可信原输入和会话/目录
  → 返回 SessionStartOutput(cwd)
  → USER 原样返回 → Gate 收集真实回执，释放在途计数
  → 原 Agent 输入继续，不增加第二次输入
```

每个激活实例至多一次通知尝试；并发输入串行等待初始化门。安装/加载只发布订阅，第一次受支持原生输入才进入此门；成功重载/重装的新实例在下一次受支持输入前获得机会，失败重载不重复旧实例。控制请求、历史查询、打开/切换会话、重连、普通回合完成不触发。本范围沿用原生 Adapter，不增加 Team、Auto-Harness、外部 Harness 或前端接线。

### 3.2 输入：每个字段表达什么、由谁填写

实际导入位置是 `jiuwenswarm.extensions_v2.event_types.session_start`。以下构造器说明平台生成后的可观察形状；USER 无需自行构造或 trigger 此生命周期事件。输入字段全部只读，没有可改写字段。

```python
from jiuwenswarm.extensions_v2.event_types.session_start import SessionStartInput

SessionStartInput(cwd="D:/projects/demo/src", surface="desktop", isInteractive=True)
```

| 字段 | 类型 / 是否总有 | 准确含义 | 应该填写什么、示例 | Handler 权限 |
| --- | --- | --- | --- | --- |
| cwd | str，必填，无省略默认 | 首次受支持输入实际运行的绝对目录，供插件定位初始化资料 | 平台从已绑定 SessionContext.cwd 填写，如 D:/projects/demo/src；不填写项目名、相对路径或 Worker 进程目录。缺失/相对目录在进入初始化尝试前拒绝 | 只读；与绑定 cwd 一致 |
| surface | RenderSurface 或 None，必填 | 初始化时已知的渲染面类型，供插件选择自己的呈现策略 | 宿主明确声明 terminal / desktop / mobile / vscode 之一；不知道时填 None。当前 Web 为 desktop；不是窗口 ID 或 ui.render 槽位 | 只读；不表示窗口已挂载 |
| isInteractive | bool，必填，无省略默认 | 本次原生请求是否支持用户交互，便于插件选择非交互准备流程 | 平台取 supports_user_interaction 的真实能力，True/False，不填字符串。当前原生 Adapter 缺省能力按原行为视为 True | 只读；不作为授权，也不保证已有 UI |

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
from jiuwenswarm.extensions_v2.event_types.session_start import SessionStartOutput

SessionStartOutput(cwd="D:/projects/demo/src")
```

| 字段 | 类型 / 返回时机 | 含义与调用方怎样使用 |
| --- | --- | --- |
| cwd | str，必填 | CORE 原样回显本次可信绝对目录。USER 返回 next 的真实结果；Gate 据此确认本通知经过 CORE。它不改变目录，不保证所有用户外部初始化均成功，也不表示 Agent 已完成回答 |

### 3.4 注册、筛选、处理示例

例如插件读取 rules.json 到自己的 self._rules，后续同一对象的 on_usage 使用这份规则。start CORE 不保存规则；同一实例的成员状态使两个回调能复用。示例包先保存首个工作区观察值，后续用量 Handler 记录初始化是否完成；跨会话状态必须另外按 session_id 管理，不把首个工作区当成所有会话的目录。

下列代码来自 [现有示例包](../../examples/session_start/extension.py)，由扩展管理入口按其 manifest 安装后发布 USER 订阅。用户传入的空 pattern 仍由宿主补上私有包匹配条件，因此只匹配本包的 `session.start`；用户还可按本事件的公开输入字段 cwd/surface/isInteractive 等值筛选。未匹配不表示以后自动补发。

```python
"""Prepare activation-owned state once, then use it on normal notifications."""

import logging

logger = logging.getLogger("jiuwenswarm.server.extensions.example.session_start")


class SessionStartExample:
    """User state belongs to this activation, not to a global session counter."""

    def __init__(self):
        self.start_count = 0
        self.workspace = None

    def register(self, registrar):
        registrar.register_event_handler("session.start", self.on_start, {}, "user")
        registrar.register_event_handler("context.usage.updated", self.on_usage, {}, "user")

    async def on_start(self, event, ops, next_handler):
        self.start_count += 1
        self.workspace = {
            "cwd": await ops.session.cwd(), "root": await ops.session.root(),
            "model": await ops.session.model(),
        }
        logger.info("session start example: package=%s surface=%s count=%s session=%s workspace=%s",
                    ops.package_metadata.package_id, event.event_input.surface,
                    self.start_count, ops.session_context.session_id, self.workspace)
        return await next_handler(event)

    async def on_usage(self, event, ops, next_handler):
        logger.info("session start example usage: session=%s ready=%s count=%s",
                    ops.session_context.session_id, self.workspace is not None, self.start_count)
        return await next_handler(event)


def create_extension():
    return SessionStartExample()
```

## 4. 方法契约

### 4.1 用户方法与平台入口

无新增 `ops.session.start()`。平台方法仍为：

```python
await manager.ensure_session_started(
    context, request_id="request-a", channel_id="web",
    is_interactive=True, surface="desktop",
)
```

context 是 Adapter 已收集的可信资料；request_id/channel_id 仍为原路由观察值，不参与公开 Event 字段。surface 是宿主声明，默认 None；is_interactive 是平台方法的 Python 参数，对应公开字段 isInteractive。方法返回本次新尝试实例的 SessionStartOutput 元组；没有新实例返回空元组。

### 4.2 旧 Handler / 回执迁移

这些生命周期特性尚在 Server !99 中修订；旧示例应按下表迁移。平台内部的 snake_case 身份、稳定视图和校验状态继续保留，不因公开字段简化而削弱隔离。

| 旧用法 | 当前写法与影响 |
| --- | --- |
| event.event_input.package_id | ops.package_metadata.package_id，读取当前真实包身份 |
| event.event_input.session_id | ops.session_context.session_id，读取可信会话身份 |
| event.event_input.project_root | await ops.session.root()，依该查询契约读取可得项目根 |
| event.event_input.is_interactive | event.event_input.isInteractive；matcher 也改为 isInteractive |
| generation / request_id / channel_id 事件字段 | 从公开输入移除；代次和请求的验证/去重仍由平台 Gate 持有 |
| result.accepted / package_id / generation / session_id / request_id | 移除；保留 await next_handler(event) 的真实结果，需要目录时读 result.cwd |

## 5. 接入与兼容方案

Worker 启动时注册事件类型、Matcher、Validator 和唯一默认 CORE；已有 `ops.build` 的 CORE 创建 Builder、BUILTIN 登记共享 Ops 方法，Registry 保存能力目录。业务发生时使用该目录创建绑定本次 SessionContext 的 Ops，不重跑 ops.build。这四条业务通知没有新增专门的 BUILTIN Handler。

USER 处理本包资源，通过 `return await next_handler(event)` 继续本条链。默认 CORE 只确认真实平台通知，不再次执行创建、切换、删除或关闭。输入均只读；提前短路伪造回执、修改输入、在 next 之后换身份无效，沿既有 Core USER 故障隔离处理。真实宿主操作的所有权和最终成功判断仍在原平台业务。

初始化门为 [management/session_start.py](../../management/session_start.py)，宿主 EventBus 注册期绑定与 matcher 为 [event_types/session_start.py](../../event_types/session_start.py)，真实 Adapter 接线为 [interface_deep.py](../../../server/runtime/agent_adapter/interface_deep.py)。默认 CORE 核对私有调用作用域里的 session_id，并核对绑定 cwd 与公开 cwd 一致，生成一次真实 cwd 回执。原激活记录和发布 ID 的去重逻辑保持；发布 ID 不再作为 Core trigger 参数。

USER 普通异常沿 Core 隔离；框架错误和原输入取消传播，不自动重放已经开始的初始化尝试。调用开始即标记已尝试，外部副作用不自动回滚。门拒绝重入，重载仍沿 Core 热切换并重新核对真实实例；在途计数成对保护卸载。

字段范围以 [S_12](../specs/S_12_session-start.md) 为准。Worker 持有唯一 EventBus；Front/Supervisor 继续沿用原请求路由。普通通知不新增聊天、历史写入或 UI 渲染。每份特性文档独立维护，不把 start、attach、detach、end 合成一个模糊的会话状态事件。

## 6. 验收与未决问题

### 2026-11-03 宿主注册期包匹配

本轮在 Swarm `feature/session-start-match-pattern-20261103`（目标基线 `4b6021ca8`）和 Core 同名独立工作树（目标基线 `45e6418c`）中，撤回 Core `trigger(handler_ids=...)`，改由 `SessionStartEventBus` 在 Runtime 发布 `session.start` Handler 时从已核实的 PackageMetadata 注入私有包匹配键。Gate 用调用局部作用域绑定目标包，不增加 `SessionStartInput` 的公开字段；matcher 先核对目标包，再按用户原有 pattern 匹配。用户仍可注册 `{}`，无需填写包 ID；保留发布 ID 仅用于激活实例版本核对与注销。

Windows / Python 3.11.15，显式 `PYTHONPATH` 指向两份候选源码。目标分支更新后，Core 相关 Events、Extension、callback、AgentRuntime 和 DeepAgent 绑定回归 **709 passed、1 skipped**；Swarm 扩展、Runtime、Worker 活性与原生生命周期相关回归 **1458 passed、5 skipped、2 deselected**。两项 deselected 为先前在未修改基线也复现的 LoopBeat 计时断言。新增回归覆盖两包空 pattern 隔离、用户 `surface` pattern 保留且不补发、私有包匹配键不能由扩展自填、重载和卸载重装。对应报告为 `core_relevant-rebased.xml`、`swarm_relevant-rebased.xml` 及同名 `.log`，均在 `D:/work_space/.codex-temp/session-start-match-pattern-20261103/`。

扩大到目标分支新增 store 用例时，Core 首轮 **761 passed、2 skipped、2 failed**，同两项 store 用例在未修改的 Core 目标基线也失败；Swarm 首轮 **1463 passed、5 skipped、2 deselected、1 failed**，失败的 store 持久化用例在未修改的 Swarm 目标基线也复现。当前通过数明确排除 Core `test_ops_store.py` 与 `core/extension/data`、Swarm `test_store_persistence.py`，不宣称全套通过；失败日志为 `core-rebased.log`、`swarm-rebased.log`，基线对照为 `swarm-target-store.log` 及 Core 目标工作树定向运行记录。初版候选基于 Core `4aceb5cd` / Swarm `b7892d368` 时的回归为 581 passed/1 skipped 与 1414 passed/5 skipped/2 deselected，仅保留在 `core-regression.xml`、`swarm-regression.xml` 中，不作为更新目标后的通过数。

初版候选的独立 Python 进程使用真实 Assembly、Registry 和磁盘包，先安装 A、再安装 B、重载 A；USER 进入顺序为 A/B/A，后续输入无重复，Core `trigger` 形参为 self/event/session，记录为同目录 `process-probe.log`。更新目标分支后，经真实 Front → Supervisor → 子 Worker 安装 A、首次原生 `chat.send`、安装 B、再次原生 `chat.send`：USER start 顺序仅为 A/B，两轮真实模型均收到 `chat.final` 与 `chat.usage_summary`，公开输入仍仅含 cwd/surface/isInteractive，真实 CORE 回执为 cwd；USER 记录的两仓源码路径指向本次候选工作树。结果见 `real-worker-155436/result.json`、USER JSONL 与原生请求响应；`cleanup.json` 确认 Worker PID 退出和端口关闭。

桌面 HTML 中 `session.start` 的完整 Gate 源码摘录与候选文件逐字一致，调用链和源码链接已按当前候选更新；Edge 无页面脚本错误，1440px/390px 视口均无横向溢出，截图与检查脚本保存在同目录。本轮未执行非 Windows 环境或远端 CI；2026-11-02 的真实模型结果只属于当时的 `handler_ids` 版本。

### 2026-11-02 Claude Mods 字段修订

四项生命周期及字段定向用例共 **94 项通过**，包含在同一次最终回归的 **1365 项通过** 内，不重复累计。最终范围包括全部 extensions_v2、全部 Runtime、Worker 关闭、原生会话删除/归档和切换接线。5 项平台条件跳过：符号链接、命名管道及 3 项 Unix socket 用例。两个未修改的 LoopBeat 50ms 计时断言在本机存在约 47ms 读数；本分支与未修改基线分别保留复现，最终显式排除两项，不改阈值、不计为通过。

真实 Front → Supervisor → 子 Worker 验证：两个独立 Worker、3 轮真实模型对话，所有生命周期 USER Handler 在真实子进程中执行；没有 mock EventBus、Ops、模型或 Adapter，也没有介入 ui.render。两个 Worker 各一次 start，后续会话切换/回合不重跑；真实输入中 cwd/surface/isInteractive 和 cwd 回执符合新格式，项目根与模型查询仍使用可信 Ops。 两个 Worker PID 均已退出、端口关闭。

现成 HTML 的 A/B 教学案例也重新验证：A 在当前真实 start/usage CORE 下共享同一实例规则；B 的 SQLite HTTP 查询通过，但其模型发现/工具调用仍为教学宿主模拟，不把它当作上述真实模型工具使用证据。

证据目录：`D:/work_space/.codex-temp/align-session-mods-fields-20261102`，包括 final.log/xml/json、contracts.xml、focused-first.xml、baseline-liveness.xml、examples-verified.json。真实进程记录在 `D:/work_space/.codex-temp/align-session-mods-fields-20261102/real-150258`：result.json、USER JSONL、原生请求/响应、explicit-detach.json、两个 cleanup.json 和源码摘要。Core 使用 !110 的 0b5dd43f179be0a41ef2bd4bf0d43950bec9a92f；本次不新增 Core API。尚未执行远端 CI 或原生非 Windows 验收。

文档按 F_16 session.append 的七段结构维护；输入/输出对象、逐字段含义/填写/用途、USER 示例、平台入口、迁移与调用链分别展开。字段修订前的旧结果保留在以下历史区，不作为本次新契约的通过证据。

文档与本地 HTML 核对通过：四份特性保持 session.append 的同一七段结构，8 个输入/输出对象实际构造、28 段 Python 示例语法、120 个本地链接通过（跨仓链接按配套 Core 核对）。HTML 的生命周期调用链、字段表和实际函数已更新，A/B 完整案例逐字保留；桌面/手机视口无页面横向溢出，复制按钮及页面脚本通过。这是独立静态文档渲染检查，不计作产品浏览器 UI 验收。

本次重点覆盖：公开字段/回执及 matcher 大小写、可信 SessionContext 保持、同实例初始化去重、多窗口与切换、过期关联/输入改写/假回执拒绝、真实运行结束的 detach/end 顺序、超时/取消和进程清理。旧日期的验证不自动算成本次重新执行。

<details><summary>此前版本的验证记录：字段或触发范围以相应日期为准</summary>

本次 2026-10-31 仅按 F_16 的写法补齐输入/输出字段、注册示例、真实触发条件、Handler 职责和完整业务案例；运行代码与契约不变。文档核对包括 dataclass 字段与示例值、源码节选、Python/YAML/JSON 示例及相对链接。下列回归和真实服务结果均保留原运行日期与范围，不宣称本次重新执行业务或模型验收。

2026-10-31 合并交付验证：本特性与会话查询文档、session.start、session.end、session.attach/session.detach 一起整理到 [Server !99](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/99)，保留四个提交和各自特性文档。合并 PR 中的 session.start 依赖 [agent-core !110](https://gitcode.com/huenrui_/agent-core/merge_requests/110) 的公开 handler_ids；必须先合入 Core !110，再合入 Server。Core 文档因与目标分支重号改为 EV-07 / F_07，接口语义不变。

本次基于 Server `c6a07f909` 和合并最新目标分支的配套 Core，Windows / Python 3.11.15 下 Server 扩展、Runtime、历史、模型上下文与 Gateway 回归 **1744 项通过、3 项跳过**；Core Events/Extension/callback **587 项通过、1 项跳过**。Gateway 的 `test_web_disconnect_unregisters_physical_subscriptions` 在未修改目标分支和合并分支上均因 SERVICE_UNAVAILABLE 失败，最终合跑显式排除该 1 项，未将其计为通过。完整命令与 XML 在 `D:\work_space\.codex-temp\consolidate-server-prs-20261031`：server-final-command.json、server-final-regression.xml、core-command.json、core-regression.xml；基线对照见 gateway-baseline.xml 与 gateway-candidate.xml。本次未重跑真实模型或浏览器，以下旧日期的真实验证保持历史归属。

原独立交付为 Server !91；合并时更新初始化测试替身以转交平台已存在的 file_service，并在实际 USER Handler 内验证历史和用量共享 Ops 也可见。初始化与固定生命周期观察者专项共 20 项通过，已包含于上述回归，不重复累计。

### 6.1 2026-10-28 单元与回归

环境：Windows、Python 3.11.15；显式 PYTHONPATH 指向本次 Swarm 工作树与配套 Core。Core 版本见第 1 节。使用真实 Registry 与用户模块发布测试，不用替代 EventBus 验证包隔离。

| 输入与前置条件 → 操作 | 预期与已验证证明 |
| --- | --- |
| A/B 两用户包 → 首次输入、重复输入、另一会话输入 | 各实例只通知一次；空 matcher 不导致旧包重跑；真实 cwd/root/model/repo 可读 |
| 已初始化包 → 新包装载、成功/失败重载、卸载重装 | 只处理新实例；失败重载保留旧实例；generation 回到 1 及空包重装仍可识别 |
| 8 个并发首次输入、用户回调查询/卸载/重载 | 串行门无重复；管理查询无死锁；在途卸载 BUSY；重载新实例被检查 |
| 用户改写输入、伪造/替换回执、重复 next、直接伪造 Event | Validator 拒绝非法结果；真实 CORE 回执保留，不可撤销已发生事实 |
| USER 抛错、框架错误、取消、同任务/子任务重入 | 按各自契约隔离或传播；计数释放；已开始通知不重放；逃逸子任务在作用域结束后不被误判重入 |
| 原生 unary/stream 请求、控制请求 | 目录/模型绑定 → 等待 start 返回 → 原 Agent 输入；控制请求不新增通知，使用应用前捕获的真实模型快照 |

`test_session_start.py` 共 15 项；原生文件新增 3 项接线用例。Server V2 与相关原生/模型兼容合跑 **253 passed**，有一项既有 Authlib 弃用警告。Core Events 单独 **112 passed**，Events/包管理/callback 合跑 **568 passed, 1 skipped**；跳过项为 Windows 创建目录 symlink 权限不足，另有既有 Pydantic 警告。

在各仓库根目录执行的命令（设置 PYTHONPATH 后）：

```text
python -m pytest -o addopts= -q tests/unit_tests/extensions_v2 tests/unit_tests/agentserver/test_deep_adapter_stream_end_final.py tests/unit_tests/agentserver/test_request_scoped_login_model.py tests/unit_tests/agentserver/test_deep_adapter_model_resolve.py tests/unit_tests/agentserver/test_deep_adapter_login_model.py tests/unit_tests/agentserver/test_agent_reload_model_scope.py tests/unit_tests/agentserver/test_cross_session_model_input.py tests/unit_tests/agentserver/test_cron_session_model_inherit.py --asyncio-mode=auto -o log_cli=false
python -m pytest -o addopts= -q tests/unit_tests/core/events tests/unit_tests/core/extension tests/unit_tests/core/runner/callback --asyncio-mode=auto -o log_cli=false
```

### 6.2 2026-10-29 真实模型与用户 Handler 验证

使用独立配置/数据目录、实际 ZIP 和真实 Front → Supervisor → child Worker；创建普通 Git 仓库子目录及独立 worktree 子目录，包在 USER 中记录进程 ID、源码位置、上下文、只读 Ops 结果与真实 CORE 回执。未手工注入 SessionContext，也未替换 Bus/Ops/Adapter/模型。实测批次 `real-090239`，使用真实 `GLM-5.2`，验证代码为 Swarm `7f3862773` / Core `6ef27f309`；两仓 8 个实现文件 SHA256 在测试中保持不变，PR04/05 模块不存在。此后只更新验证文档。

| 真实操作 | 初始化观察 | 模型与后续 Handler 结果 |
| --- | --- | --- |
| alpha 首次原生输入 | A generation=1 初始化一次，随包示例执行一次；cwd/root/model/repo 与真实回执正确 | 有模型回复与有效用量；A 的 on_usage 在初始化后执行，ready=True、count=1 |
| alpha 再输入 | A 不重复初始化 | 有模型回复；A 的 on_usage 仍为 ready=True、count=1 |
| WebSocket 重连后 beta 输入 | A 不重复，激活实例不随会话/连接切换 | 有模型回复；USER 读取当前 beta 的真实 cwd/root/model/repo |
| 加载 B 后 beta 输入 | 仅 B generation=1 初始化，A 不重复 | 有模型回复；A/B 的 on_usage 均读取各自已准备状态 |
| 重载 A 后 alpha 输入 | A generation=2 初始化，B 不重复 | 有模型回复；新 A 与 B 的 on_usage 均为 ready=True、count=1 |
| 卸载重装 A 后 alpha 输入 | A 新实例 generation=1 仍初始化 | 有模型回复；重装 A 的 on_usage 在新初始化后执行 |
| 全部卸载后再次输入 | USER 初始化/用量日志不再增加 | 原生模型回复和用量统计正常；退出后 Worker 已结束、监听端口已关闭 |

合计 **7 个真实模型回合全部成功**，均收到非空 `chat.delta`、`chat.final` 和正数 `chat.usage_summary`，回复包含 `Start validation completed`。A 初始化 3 次（代次 1/2/1）、B 1 次、随包示例 1 次。所有应参与的 USER on_usage 均观察到此前初始化已完成、count=1、默认 CORE accepted=True；卸载后的回合不再调用这些用户 Handler。随包示例日志也已验证 `ready=True count=1`。

本次通过范围覆盖初始化前后顺序、真实模型执行、实际用量通知、用户只读 Ops 和 Handler 状态延续，以及退出清理。单元与回归数量仍是第 6.1 节注明的 2026-10-28 历史运行，不因文档修订宣称重跑。浏览器 UI、远端 CI 和非 Windows 实机未执行，不在本次结论内。用户外部资源是否准备成功，仍由各自 Handler 确认；本事件 accepted 的契约不变。

文档按用户指定《openjiuwen 事件与方法特性文档编写指南》维护；实际模型调用、用量 Handler 和清理证据已补齐，本次补验项已完成。

2026-11-02 文档整理：以原 session.append 为参照，统一对象示例、五列通知输入表、三列输出表、会话资料说明及具体用户 Handler 案例；保留所有字段来源、默认／缺失、权限、超时和历史验证记录。只改文档，当前契约及 Core !110 前置不变。本轮核对结构、字段、示例与真实默认 CORE 回执、链接及差异，不重跑真实业务、模型或浏览器验收。

</details>

## 7. 参考资料

- [Claude Code Mods 固定类型声明](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)，SessionStartInput/Result；参照小写 Mods 事件。
- [主责规约 S_12](../specs/S_12_session-start.md)。
- [事件定义与校验](../../event_types/session_start.py)、[默认 CORE](../../event_handlers/session_start.py)。
- [示例包说明](../../examples/session_start/README.md)。
- [session.append 文档格式基准](F_16_session-history-ops.md)。
