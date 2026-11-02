# session.attach 特性设计

| 项 | 值 |
| --- | --- |
| 编号 | F_25 / session-attach |
| 建档日期 | 2026-10-29 |
| 归档日期 | 2026-10-29 |
| 后续修订 | 2026-11-02，按既定 session.append 七段格式对齐 Claude Mods 字段；旧字段迁移见第 4.2 节，验证按版本分别记录 |
| 计划与进度 | [EXTV2-20 / Server PR08](../designs/extensions-v2-development-plan.md#extv2-20-界面与主会话关联建立通知) |
| 主责规约 | [S_14](../specs/S_14_session-attach.md) |

## 1. 范围、状态与依据

本项在具体 Web 视图与有效主会话的关联提交后，通知用户准备该关联的资料或资源。本次把公开输入收敛为 surface/clientId/viewport，默认输出收敛为 clientId；会话、渠道和关联版本保留为可信上下文或宿主状态。原 session.create/session.switch 接线保持，不改前端、不新增同名 Ops。

对照 [Claude 官方类型声明](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)，固定提交 bf2323b3021c9d3db7a31e1e7d61068c3f704497，文件头 2.1.277。2026-11-02 重新下载官方 main 与固定文件，内容一致；这是此次字段核对的依据，不混用传统大写 SessionStart/SessionEnd Hooks。本地事件仍是 Python frozen dataclass，None 表示对应可选值缺失，并非直接接收任意 TypeScript/JSON 对象。

## 2. 能力说明与 Claude 对照

`session.attach` 告知插件“这个具体客户端已经加入该会话”。USER 可按渲染面筛选、使用绑定新会话的 Ops 查询资料，为本次关联建立缓存或订阅，然后 next；CORE 确认关联和目标仍然有效，回显 clientId。插件自己的前端能力决定怎样展示，事件本身不自动 ui.render。

| 项 | Claude Mods 2.1.277 | 本次实现与使用影响 |
| --- | --- | --- |
| 输入 | surface / clientId / 可选 viewport | 名称与语义对齐；viewport 在 Python 中未知为 None |
| 输出 | clientId | 仅回显真实客户端，不再返回 accepted/session_id/attachment_id |
| 客户端身份 | 同一种 surface 上可有多个客户端 | clientId 为 channel:view_id；同一窗口切换会话保持客户端身份，关联版本另存 |
| 触发事实 | 远程客户端声明加入或首次请求绘制 | 本地沿用创建/切换提交事实；不把 ui.ready、模型执行或传输重连冒充新关联 |

`surface` 与 `clientId` 是两个维度：前者是渲染面类型，后者是具体客户端。当前 Web 宿主明确使用 `desktop` 渲染面协议，不通过浏览器 UA 猜设备；手机浏览器不会仅因屏幕小就自动变成 mobile。其他宿主只有在明确声明 terminal/mobile/vscode 等类型后才可报告。start 不知道渲染面时为 None；attach 的 surface 必须已知，否则宿主不应伪造一次成功客户端关联。

当前关联参与者的默认渠道声明为 `web → desktop`，其他渠道默认不发 attach。clientId 使用 `channel_id:view_id`，例如 `web:view-main`，避免不同渠道的同名窗口冲突；不是每次重新生成的 attachment_id。旧调用缺少 view_id 时保留原平台降级标识，多个窗口应各自提供稳定且不同的 view_id。

## 3. 事件契约

### 3.1 触发前后与粒度

```text
前端创建/切换请求 + 稳定 view_id → Front / Supervisor → Worker
  → Runtime 原 prepare / commit → foreground_changed(visible=True)
  → 平台确认主会话存在且可进入，构造可信 SessionContext
  → 关联表提交 channel+view 的新目标、clientId 和私有关联版本
  → 有旧关联时先发旧 session.detach；再次确认新关联仍有效
  → 构造 SessionAttachInput(surface, clientId, viewport)
  → await bus.trigger(Event("session.attach", input), 新会话 context)
  → Core 为 USER 创建 Ops，绑定新会话、当前包和只读关联资料
  → USER 读取项目根等可得资料 → receipt = await next_handler(event)
  → 剩余 Handler → session_attach_core 核对原输入、会话和当前关联/目标
  → SessionAttachOutput(clientId) 返回 USER
  → USER 按只读 attachment_id 保存本包资料，原样返回真实回执
  → 平台消费回执，原创建/切换继续
```

每个 channel+view 的一次新主会话关联通知一次。不同窗口分别通知；相同现存关联重放、稳定 view_id 的重连不重复通知；离开再回来是新关联。create 保持原响应先交付、后后台提交，switch 保持原提交等待。仅包安装或 ui.ready 不补发已有关联。

### 3.2 输入：每个字段表达什么、由谁填写

实际导入位置是 `jiuwenswarm.extensions_v2.event_types.session_attach`。以下构造器说明平台生成后的可观察形状；USER 无需自行构造或 trigger 此生命周期事件。输入字段全部只读，没有可改写字段。

```python
from jiuwenswarm.extensions_v2.event_types.session_attach import SessionAttachInput

SessionAttachInput(surface="desktop", clientId="web:view-main", viewport=None)
```

| 字段 | 类型 / 是否总有 | 准确含义 | 应该填写什么、示例 | Handler 权限 |
| --- | --- | --- | --- | --- |
| surface | RenderSurface，必填 | 该客户端使用哪种渲染面，便于插件选择兼容的展示逻辑 | 宿主明确填写 terminal / desktop / mobile / vscode；当前 Web 声明 desktop。不能填写 view-main、会话 ID、槽位 ID 或 web 渠道名 | 只读 |
| clientId | str，必填，无省略默认 | 加入该会话的具体客户端身份；同一种 surface 上可有多个客户端 | 当前宿主从 channel_id + ":" + view_id 生成，如 web:view-main。不是 attachment_id；同一窗口切换会话仍是同一个客户端 | 只读；不作为授权凭据 |
| viewport | RenderViewport 或 None，可省略，默认 None | 宿主已知的整个渲染面的字符尺寸和可选全屏状态，供插件布局判断 | 真实上报时填写 RenderViewport(columns=120, rows=40, isFullscreen=False)。当前原生入口未采集，填 None；不用浏览器像素或虚构默认值 | 对象及嵌套字段只读 |

#### viewport 的全部字段

`viewport` 是 `RenderViewport` 对象；Python 的 None 表示该可选对象未上报。当前原生 Web 关联入口未采集字符尺寸，所以实际通知为 None；没有默认填入 80×24，也不拿浏览器像素冒充字符格。宿主以后提供真实测量时使用下列形状：

```python
from jiuwenswarm.extensions_v2.event_types.session_surface import RenderViewport

RenderViewport(columns=120, rows=40, isFullscreen=False)
```

| 字段 | 类型 / 默认值 | 准确含义、应填写什么及作用 | Handler 权限 |
| --- | --- | --- | --- |
| columns | int，必填 | 整个渲染面的横向字符格数，须为正整数，如 120。用于布局换行与宽度判断；不是像素宽度，也不是某张卡片的宽度 | 只读 |
| rows | int，必填 | 整个渲染面的纵向字符格数，须为正整数，如 40。用于读取当前可见尺寸；不是某组件剩余可用行数 | 只读 |
| isFullscreen | bool 或 None，默认 None | 宿主上报的全屏呈现状态：True/False 是明确的状态，None 是尚未上报。不能把未知填成 False，也不据此承诺已打开侧栏 | 只读 |

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
from jiuwenswarm.extensions_v2.event_types.session_attach import SessionAttachOutput

SessionAttachOutput(clientId="web:view-main")
```

| 字段 | 类型 / 返回时机 | 含义与调用方怎样使用 |
| --- | --- | --- |
| clientId | str，必填，无省略默认 | 默认 CORE 原样回显本次客户端。USER 原样返回 next 的真实回执；平台继续核对当前关联和目标。它不是切换命令，不证明插件已展示成功，也不保证返回后永远不再切换 |

### 3.4 注册、筛选、处理示例

例如“会话项目资料”插件：窗口 V 进入 A 后，on_attach 通过 ops.session.root() 读取 A 的可得项目根。插件可以按上下文中的 attachment_id 保存本次关联资料；随后切换 B，由旧 detach 清理 A 的关联资源，再由 B 的 attach 准备新资料。默认 CORE 不替插件准备前端，也不再次切换会话。现成示例包用日志验证 root 和 clientId。

下列代码来自 [现有示例包](../../examples/session_attach/extension.py)，由扩展管理入口按其 manifest 安装后发布 USER 订阅。空 matcher 匹配本次候选内的本事件；可按公开输入字段等值筛选，例如 surface/clientId/isInteractive/reason，大小写必须与具体事件的字段一致。未匹配不表示以后自动补发。

```python
"""Observe committed views without starting chat or modifying the host."""

import logging

logger = logging.getLogger("jiuwenswarm.server.extensions.example.session_attach")


class SessionAttachExample:
    """User-owned initialization for a view entering a main Session."""

    def register(self, registrar):
        registrar.register_event_handler("session.attach", self.on_attach, {}, "user")

    async def on_attach(self, event, ops, next_handler):
        root = await ops.session.root()
        receipt = await next_handler(event)
        logger.info("session attach example: session=%s surface=%s client=%s root=%s",
                    ops.session_context.session_id, event.event_input.surface,
                    receipt.clientId, root)
        return receipt


def create_extension():
    return SessionAttachExample()
```

## 4. 方法契约

### 4.1 用户方法与平台入口

无新增 `ops.session.attach()`。宿主继续调用 `ExtensionSessionAttachParticipant.foreground_changed(event)`；event 来自 Runtime 已提交的前台变化，包含原稳定 view_id。默认宿主渠道声明为 web→desktop，不从客户端可改事件推断身份。可选真实 viewport 由平台关联提交函数接收；当前原生接线不提供该测量，不新增前端采集。

### 4.2 旧 Handler / 回执迁移

这些生命周期特性尚在 Server !99 中修订；旧示例应按下表迁移。平台内部的 snake_case 身份、稳定视图和校验状态继续保留，不因公开字段简化而削弱隔离。

| 旧用法 | 当前写法与影响 |
| --- | --- |
| input.surface_id | input.clientId 是具体客户端；原 view_id 在 ops.session_context.metadata["session_attachment"]["view_id"] |
| input.session_id | ops.session_context.session_id，绑定新目标 |
| input.attachment_id / previous_session_id / channel_id / mode | 从 ops.session_context.metadata["session_attachment"] 读取对应只读本地资料 |
| 把 surface 当窗口 ID | surface 只填写 desktop 等渲染面类型；具体客户端读 clientId |
| result.accepted / session_id / surface_id / attachment_id | 移除；返回 next 的真实 clientId 回执。关联版本仍从可信上下文取得 |
| matcher 使用旧 surface_id / session_id | 改用 surface / clientId / viewport；会话相关处理通过绑定上下文判断 |

## 5. 接入与兼容方案

Worker 启动时注册事件类型、Matcher、Validator 和唯一默认 CORE；已有 `ops.build` 的 CORE 创建 Builder、BUILTIN 登记共享 Ops 方法，Registry 保存能力目录。业务发生时使用该目录创建绑定本次 SessionContext 的 Ops，不重跑 ops.build。这四条业务通知没有新增专门的 BUILTIN Handler。

USER 处理本包资源，通过 `return await next_handler(event)` 继续本条链。默认 CORE 只确认真实平台通知，不再次执行创建、切换、删除或关闭。输入均只读；提前短路伪造回执、修改输入、在 next 之后换身份无效，沿既有 Core USER 故障隔离处理。真实宿主操作的所有权和最终成功判断仍在原平台业务。

[平台关联表及通知](../../../server/runtime/session/attach_notifications.py) 以私有 SessionAssociation 保存真实目标、view_id、attachment_id 和上下文。公开输入由 event_input() 单独构造；CORE 使用私有调用状态核对绑定会话和关联版本，不从公开 clientId 猜目标。默认 [session_attach_core](../../event_handlers/session_attach.py) 仅确认事实。

等待预算默认 2 秒。USER 错误或通知超时不恢复已提交关联；原调用取消继续传播。超时使作用域失效并请求取消，忽略取消的任务不能产生迟到有效 CORE 回执，在途计数直到实际退出才归零。USER 外部副作用不自动回滚，按 attachment_id 清理自己的资源。

字段范围以 [S_14](../specs/S_14_session-attach.md) 为准。Worker 持有唯一 EventBus；Front/Supervisor 继续沿用原请求路由。普通通知不新增聊天、历史写入或 UI 渲染。每份特性文档独立维护，不把 start、attach、detach、end 合成一个模糊的会话状态事件。

## 6. 验收与未决问题

### 2026-11-02 Claude Mods 字段修订

四项生命周期及字段定向用例共 **94 项通过**，包含在同一次最终回归的 **1365 项通过** 内，不重复累计。最终范围包括全部 extensions_v2、全部 Runtime、Worker 关闭、原生会话删除/归档和切换接线。5 项平台条件跳过：符号链接、命名管道及 3 项 Unix socket 用例。两个未修改的 LoopBeat 50ms 计时断言在本机存在约 47ms 读数；本分支与未修改基线分别保留复现，最终显式排除两项，不改阈值、不计为通过。

真实 Front → Supervisor → 子 Worker 验证：两个独立 Worker、3 轮真实模型对话，所有生命周期 USER Handler 在真实子进程中执行；没有 mock EventBus、Ops、模型或 Adapter，也没有介入 ui.render。5 次 attach 均只暴露 surface/clientId/viewport，CORE 只回显 clientId；首次进入、切换、多窗口、显式解除后重新进入及新 Worker 恢复均经过真实原生入口，root 可读。 两个 Worker PID 均已退出、端口关闭。

现成 HTML 的 A/B 教学案例也重新验证：A 在当前真实 start/usage CORE 下共享同一实例规则；B 的 SQLite HTTP 查询通过，但其模型发现/工具调用仍为教学宿主模拟，不把它当作上述真实模型工具使用证据。

证据目录：`D:/work_space/.codex-temp/align-session-mods-fields-20261102`，包括 final.log/xml/json、contracts.xml、focused-first.xml、baseline-liveness.xml、examples-verified.json。真实进程记录在 `D:/work_space/.codex-temp/align-session-mods-fields-20261102/real-150258`：result.json、USER JSONL、原生请求/响应、explicit-detach.json、两个 cleanup.json 和源码摘要。Core 使用 !110 的 0b5dd43f179be0a41ef2bd4bf0d43950bec9a92f；本次不新增 Core API。尚未执行远端 CI 或原生非 Windows 验收。

文档按 F_16 session.append 的七段结构维护；输入/输出对象、逐字段含义/填写/用途、USER 示例、平台入口、迁移与调用链分别展开。字段修订前的旧结果保留在以下历史区，不作为本次新契约的通过证据。

文档与本地 HTML 核对通过：四份特性保持 session.append 的同一七段结构，8 个输入/输出对象实际构造、28 段 Python 示例语法、120 个本地链接通过（跨仓链接按配套 Core 核对）。HTML 的生命周期调用链、字段表和实际函数已更新，A/B 完整案例逐字保留；桌面/手机视口无页面横向溢出，复制按钮及页面脚本通过。这是独立静态文档渲染检查，不计作产品浏览器 UI 验收。

本次重点覆盖：公开字段/回执及 matcher 大小写、可信 SessionContext 保持、同实例初始化去重、多窗口与切换、过期关联/输入改写/假回执拒绝、真实运行结束的 detach/end 顺序、超时/取消和进程清理。旧日期的验证不自动算成本次重新执行。

<details><summary>此前版本的验证记录：字段或触发范围以相应日期为准</summary>

本次 2026-10-31 仅按 F_16 的写法补齐输入/输出字段、注册示例、真实触发条件、Handler 职责和完整业务案例；运行代码与契约不变。文档核对包括 dataclass 字段与示例值、源码节选、Python/YAML/JSON 示例及相对链接。下列回归和真实服务结果均保留原运行日期与范围，不宣称本次重新执行业务或模型验收。

2026-10-31 合并交付验证：本特性与会话查询文档、session.start、session.end、session.attach/session.detach 一起整理到 [Server !99](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/99)，保留四个提交和各自特性文档。合并 PR 中的 session.start 依赖 [agent-core !110](https://gitcode.com/huenrui_/agent-core/merge_requests/110) 的公开 handler_ids；必须先合入 Core !110，再合入 Server。Core 文档因与目标分支重号改为 EV-07 / F_07，接口语义不变。

本次基于 Server `c6a07f909` 和合并最新目标分支的配套 Core，Windows / Python 3.11.15 下 Server 扩展、Runtime、历史、模型上下文与 Gateway 回归 **1744 项通过、3 项跳过**；Core Events/Extension/callback **587 项通过、1 项跳过**。Gateway 的 `test_web_disconnect_unregisters_physical_subscriptions` 在未修改目标分支和合并分支上均因 SERVICE_UNAVAILABLE 失败，最终合跑显式排除该 1 项，未将其计为通过。完整命令与 XML 在 `D:\work_space\.codex-temp\consolidate-server-prs-20261031`：server-final-command.json、server-final-regression.xml、core-command.json、core-regression.xml；基线对照见 gateway-baseline.xml 与 gateway-candidate.xml。本次未重跑真实模型或浏览器，以下旧日期的真实验证保持历史归属。

原 attach/detach 合并交付为 Server !95；本次保留同一个关联服务、独立事件与文档，并与 session.end 固定删除观察者组合。前端、共享 SessionFileService 和已合入历史/用量能力沿用当前目标分支。

以下 378 项与原 real-195031 是 PR08 首次提交的历史证据；本轮 PR08/11 在同一 PR !95 交付，追加解除语义由 [F_26](F_26_session-detach.md) / [S_15](../specs/S_15_session-detach.md) 单独维护。当前联合源码的 446 项回归与 6 次 attach / 5 次 detach 真实验证见 F_26 第 6 节。

2026-10-29，Windows / Python 3.11.15，Server 基于上文固定基线并包含首次 PR08 变更，Core 为 ead700d7d18a247968d64de54918cf5d201a207a。378 项扩展、Runtime、创建/切换提交、KVC 参与者和架构回归通过，无跳过；另单独复验本特性及相关生命周期 27 项通过，包含在前述范围中、不重复累计。只有外部 Authlib 弃用提示，未运行格式化或静态检查工具。

```powershell
python -m pytest -o addopts= -q tests/unit_tests/extensions_v2 tests/unit_tests/runtime/test_session_attach_provisioner.py tests/unit_tests/runtime/test_session_create_provisioner.py tests/unit_tests/runtime/test_session_switch_provisioner.py tests/unit_tests/runtime/test_session_provision_contract.py tests/unit_tests/runtime/test_session_lifecycle_participants.py tests/unit_tests/runtime/test_runtime_service.py tests/unit_tests/runtime/test_runtime_architecture.py --asyncio-mode=auto -o log_cli=false
python -m pytest -o addopts= -q tests/unit_tests/extensions_v2/test_session_attach.py tests/unit_tests/runtime/test_session_attach_provisioner.py tests/unit_tests/runtime/test_session_lifecycle_participants.py --asyncio-mode=auto -o log_cli=false
```

| 场景 | 操作和预期 | 实际证据 |
| --- | --- | --- |
| 没有用量也能读取 Ops | 真用户包接收 attach，读取 root/cwd/model，原样 next 得到默认回执 | Registry/EventBus 定向测试；真实创建后首个通知早于所有 usage |
| 多视图、切换、重放 | 两视图进入同会话独立通知；同一关联重放不重复，切换再切回新 ID | 定向与真实 6 次关联记录、稳定 view_id 重连 |
| 真实提交接线 | prepare/abort 不通知，commit 后才有真实表项和 CORE 回执，重复 commit 不重放 | test_session_attach_provisioner；真实 WebSocket 创建/切换经原 IPC |
| 防改写/伪造 | 6 个输入字段、提前成功、后置修改和 int/bool 替换无效 | 参数化用户包测试核对实际调用状态中的 CORE 回执 |
| 失效关联/目标 | 关联已被替换、或 USER 期间目标目录已移除，不得取得迟到有效 CORE 回执 | 定向竞态和目标删除；真实缺失/归档目标不通知 |
| 超时与取消 | 保留已提交事实；超时任务无迟到确认，包在途计数最终释放 | 短预算构造、忽略取消和原调用取消测试 |
| KVC 共存 | 开关、suspend、lease release 不移除固定观察者，不改变输入意图列表 | 实际 KVCacheApplicationOwner 和 Registry 回归 |
| 缺失/未跟踪范围/卸载 | 无效 view、Team 不通知，未安装或卸载后原业务仍工作 | 定向测试与真实包卸载后正常切换 |

真实服务证据目录：`D:\work_space\.codex-temp\server-session-attach-event-20261029\real-195031`。安装现成示例和验收用户 ZIP，经真实 Front → Supervisor → 子 Worker 完成创建/切换；观察 6 个独立关联 ID、正确会话/视图/项目根和默认 CORE 回执。首次通知在没有任何用量与产品历史消息时发生；一个主会话可关联两个 view_id，同视图切换再切回正确记录前一目标，稳定 view_id 重连不重放。缺失/归档目标不产生 attach，原创建/切换协议保持原样。

单独发送一次真实 Qwen3.8-Flash 回合，得到有效用量；这次聊天不会多发 attach，之后新视图可正常关联。不是靠聊天或手工 notify 提供第一次上下文。未 mock Ops、模型或 Adapter，未手工构造 SessionContext。现成示例日志 6 次 accepted=True；卸载后默认切换正常。result.json、user-handler-results.jsonl、wire.jsonl、source-before/source-after 收据保存来源和一致性，cleanup.json 确认 Worker 退出和端口关闭。

边界：内存事实不跨 Worker/Runtime 重启保存，当时 view 关闭的显式撤销尚未接入，本轮 PR11 已在同 PR 增补，详见 S_15；表项不是物理在线证明。首次 PR08 不提供 viewport、surfaces Ops、detach 事件、Team/子 Agent 或产品浏览器视觉验收；当前 detach 由 F_26 独立说明。没有非 Windows 实机、远端 CI、大规模视图性能或跨重启 exactly-once 证据。2 秒仅为事件等待预算，不是整个文件校验或创建/切换的耗时上限。外部初始化副作用可能已经发生，平台不承诺回滚。

2026-11-02 文档整理：以原 session.append 为参照，统一对象示例、五列通知输入表、三列输出表、会话资料说明及具体用户 Handler 案例；保留所有字段来源、默认／缺失、权限、超时和历史验证记录。只改文档，当前契约及 Core !110 前置不变。本轮核对结构、字段、示例与真实默认 CORE 回执、链接及差异，不重跑真实业务、模型或浏览器验收。

</details>

## 7. 参考资料

- [Claude Code Mods 固定类型声明](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)，SessionAttachInput/Result；参照小写 Mods 事件。
- [主责规约 S_14](../specs/S_14_session-attach.md)。
- [事件定义与校验](../../event_types/session_attach.py)、[默认 CORE](../../event_handlers/session_attach.py)。
- [示例包说明](../../examples/session_attach/README.md)。
- [session.append 文档格式基准](F_16_session-history-ops.md)。
