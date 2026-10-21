# JiuwenSwarm Extensions V2 设计概览

## 文档状态与阅读说明

| 项 | 说明 |
| --- | --- |
| 类型 | 目标设计草案；接口尚未定稿，能力尚未实现 |
| 最近一次修订日期 | 2026-10-21 |
| 维护责任 | Extensions V2 模块维护者；跨模块约定由 agent-core 与 JiuwenSwarm 相关维护者共同确认，具体负责人待认领 |
| 所属模块 | `jiuwenswarm/jiuwenswarm/extensions_v2/` |
| 详细设计 | [详细架构](extensions-v2-architecture.md) |
| 公共机制依据 | agent-core [Events 架构](../../../../../agent-core/openjiuwen/core/events/docs/designs/events-architecture.md)、[Extension 架构](../../../../../agent-core/openjiuwen/core/extension/docs/designs/extension-architecture.md)；均为目标设计草案 |
| 模板与目录规范 | [Overview 模板](../../../docs/templates/overview-template.md)、[模块维护规范](../../AGENTS.md) |
| 关键前提 | 公共扩展机制、业务扩展点的事件触发及 WebUI 传输适配仍待建设与验证，见第 5 节 |

本文面向首次了解方案的开发、评审和测试人员，说明 Extensions V2 的架构分层、模块职责，以及包管理、事件调用和UI界面呈现如何协作。第 1～5 节依次介绍目标与范围、核心模型、主要流程、关键约束和落地条件。

概览保留整体设计结论；模块工作方式、接入步骤和异常处理在架构中展开，阅读入口集中在第 5.3 节。文中的能力与事件名称用于解释方案，尚非已发布接口。

## 1. 目标与范围

### 1.1 定位与目标

Extensions V2 是 JiuwenSwarm 接入 agent-core 通用扩展机制的产品适配层。它让扩展包在平台开放的业务处理点参与工作，并通过受控入口使用工具、会话、UI界面和渠道等产品能力。

这层适配要解决两个问题：将产品的业务处理点接入统一事件调用链，让扩展能够参与处理；将产品服务封装为 Ops 能力，让扩展在当前调用上下文中使用。包的加载与注册、事件的匹配与调用由 agent-core 提供，产品事件、Ops 能力及业务流程中的事件触发由 JiuwenSwarm 接入。

### 1.2 职责与范围边界

agent-core 提供包管理和事件调用机制，JiuwenSwarm 将它们接到产品服务，扩展包实现自己的业务逻辑。扩展与平台通过事件处理器参与调用链，处理器遵循 `EventHandler` 回调约定，下文简称 Handler。Handler 在 AgentServer 的业务运行时执行；跨服务通信复用 AgentServer Front、Gateway 和 WebClient 的传输链路，每个包无需建立专用 WebSocket 服务。

工具、文件或渠道的实际操作仍由所属服务完成。WebUI 是按需使用的呈现能力；没有UI界面的扩展也可以独立参与业务。

### 1.3 当前范围与后续扩展

本期以**本地后端包启动加载、携带可信会话的业务事件**为基础，进一步设计 WebUI 的 JSON 树呈现与交互。在线卸载、重载、Gateway 渠道能力及旧扩展迁移分别验证后开放；这些是规划范围，当前实现状态见第 5.1 节。

不同 Harness（智能体执行框架）、同步／异步／流式工具路径及非 Web 渠道需逐项接入。新增渠道也需单独验证实际收发与 V2 调用链。

远程包市场、自动联网安装依赖、Web前端执行任意插件代码、跨进程事件总线、持久事件重放、崩溃续跑和无中断热更新不在本期范围。

## 2. 核心模型与职责

### 2.1 总体关系

整体按职责分为三层：**agent-core 提供通用机制，`extensions_v2` 完成产品适配，扩展包提供业务逻辑。** AgentServer 装配这些模块；工具、会话等所属业务服务负责实际操作；WebUI 和传输组件负责可选的UI界面交互。

下图表示逻辑调用和注册关系，框内模块不要求独立部署。包管理负责使扩展可用，事件调用负责每次业务处理；扩展需要UI界面时，再使用 WebUI 能力。

```mermaid
flowchart TB
    Host["AgentServer 生命周期<br/>准备 EventBus"] -->|传入同一 EventBus<br/>就绪后提交包路径| Registry
    subgraph Core["agent-core：通用扩展机制"]
        Registry["Extension / Registry<br/>包加载、注册归属与清理"]
        Bus["Events / EventBus<br/>匹配 Handler、构建 Ops、执行调用链"]
        Registry -->|通过事件完成包管理| Bus
    end
    subgraph Adapter["extensions_v2：产品适配"]
        Catalog["产品注册函数<br/>事件类型及 Core Handler<br/>BUILTIN Handler / Ops Build Handler"]
        Entry["业务逻辑中的扩展点<br/>构造事件与 SessionContext，保留原请求授权"]
        UI["WebUI 服务（可选）<br/>可用UI界面目录、视图绑定与树校验"]
    end
    Host -->|初始化 Registry 前直接调用| Catalog
    Catalog -->|注册产品事件与 Handler| Bus
    Entry -->|调用 trigger 触发事件| Bus
    Bus -->|调度| Handlers["扩展包与平台 Handler"]
    Handlers -->|Ops 能力调用或 Core Handler 调用| Biz["所属业务服务"]
    Handlers -->|UI界面能力| UI
    UI -->|允许的 UI 事件| Bus
    UI <-->|经 Front、Gateway、WebClient 传输| FE["Web前端视图"]
```

| 参与方 | 主要职责 |
| --- | --- |
| AgentServer | 创建业务运行实例，准备 EventBus，直接执行产品注册后将同一实例传给 Registry；核对包管理结果，跟踪并等待自身业务调用结束，协调启停 |
| `extensions_v2` | 汇总产品注册函数，由 AgentServer 直接调用，注册产品事件及 Core Handler、BUILTIN Handler 和 Ops Build Handler；接入业务事件触发、上下文和 WebUI |
| agent-core Extension / Events | Registry 在传入的 EventBus 上补充管理事件，Extension 管理包加载、注册归属与清理；Events 匹配 Handler 并执行调用链 |
| 所属业务服务 | 完成实际操作，检查业务权限，保存并确认业务结果 |
| 扩展包 | 注册业务 Handler，通过开放事件参与处理、通过 Ops 使用能力，维护自身业务状态 |
| AgentServer WebUI 服务与Web前端 | 服务端管理可呈现内容、视图关系及下发内容；Web前端负责挂载、显示和采集交互 |
| Front、Gateway、WebClient | 认证、路由或传递请求、响应和主动更新；Gateway 的渠道适配器连接外部平台 |

代码依赖沿 `extensions_v2 → agent-core Extension → agent-core Events`，产品适配也可直接使用 Events 公共入口；agent-core 不反向依赖 JiuwenSwarm。Registry 与 Handler 位于 AgentServer 的业务运行时，Front 负责协议和连接；事件调用发生在进程内，跨进程或跨服务消息由 AgentServer Front、Gateway 和 WebClient 显式适配。

业务结果沿调用链返回触发事件的业务逻辑；UI界面更新通过独立消息发送，不能依赖请求响应总先到达。包加载成功不会自动触发业务，业务处理也不要求先有UI界面。

### 2.2 对象及事实权威

| 核心对象 | 含义与管理方 |
| --- | --- |
| 扩展包与注册 | 包组织代码和声明，可包含多个后端扩展单元；单元注册 Handler，包可选声明扩展UI界面项。agent-core 保存生效、注册归属和清理记录，AgentServer 据此判断可用性 |
| Event、EventBus 与 Handler | Event 表达一次处理请求；Handler 是遵循 `EventHandler` 回调约定的事件处理器，由 EventBus 根据注册匹配并调用。同一条注册可供多次事件调用复用 |
| SessionContext、Ops 与 next | 业务逻辑在触发事件前，根据服务端会话记录构造只读 SessionContext，同链共享；每次进入普通 Handler 前独立构建 Ops，包身份由 agent-core Extension 的可信适配提供，不能仅凭 Events 注册记录取得。Handler 通过 Ops 使用平台能力，通过 next 推进当前调用链，二者随该 Handler 退出而失效；适配分工见[架构第 4.3.1 节](extensions-v2-architecture.md#431-能力函数build-handler-与业务-handler-的分工) |
| 扩展UI界面项与 slot | 扩展UI界面项是包提供的面板、卡片等UI界面内容；扩展UI界面声明说明其标识、展示信息、挂载位置及渲染方式。slot 是产品预置的挂载位置；服务端提供可用目录，Web前端选择兼容内容 |
| 视图实例 | 用户打开面板时，Web前端在页面预留区域（slot）创建的具体显示对象，后文简称“视图”；同一个报告面板在不同会话打开，会形成不同视图，各自维护显示和交互状态 |
| 视图绑定与渲染帧 | AgentServer WebUI 服务校验面板的挂载请求后，保存该视图与Web前端连接、AgentServer 实例、会话、包、UI界面项和 slot 的对应记录，称为“视图绑定”；当前有效的记录称为“活跃绑定”。服务端据此核对交互来源和更新目标，Web前端接收渲染帧并更新对应视图 |
| 业务结果 | 工具、文件或外部操作的实际结果，以所属业务服务的记录或外部回执为准 |

每个事件在 CORE 层有唯一的 Core Handler，负责该事件的默认动作。本文将 `ops.build` 以外的事件称为普通事件，参与这些事件的 USER、BUILTIN 层 Handler 及 Core Handler 统称普通 Handler。`ops.build` 的 Handler 称为 Build Handler，只负责收集能力，构建时不接收会话和可用 Ops，也不为自身递归构建 Ops；构建流程见[架构第 4.3.2 节](extensions-v2-architecture.md#432-能力注册与构建接入)。

这些对象形成三个范围：包及注册属于 AgentServer 运行实例，一次事件及其 Handler 拥有本次调用上下文，每个已打开的面板拥有独立的视图和视图绑定。例如，会话 A、B 分别打开同一个报告面板时，Web前端创建两个视图，服务端保存两条记录；刷新 A 的面板时，通过 A 的记录核对会话并把更新发回 A 的面板。包是否可用、一次业务是否完成和面板是否显示内容，需要分别判断。

### 2.3 贯穿设计的原则

- **实际操作由所属服务完成。** 每个事件的 Core Handler 负责其默认动作，触发事件的业务逻辑不再重复执行同一动作；扩展 Handler 合法短路时，也不补执行 Core Handler。
- **上下文随调用绑定。** 每个实际进入的普通 Handler 使用独立 Ops；共享业务状态的隔离与并发仍由所属模块负责。
- **结果分环节确认。** 包生效、事件返回、业务完成和页面显示各有依据，后续失败不能抹去已发生的事实。

### 2.4 集成 agent-core 实现扩展机制时的接入工作

<a id="241-将产品能力开放为-ops"></a>
<a id="242-新增产品事件类型"></a>

产品接入包含两件事：**用事件开放扩展可以参与的处理点，用 Ops 开放 Handler 可以调用的产品能力**。

**新增产品事件。** 明确事件的输入、返回和 Core Handler，由 AgentServer 在 Registry 初始化前注册到 EventBus；业务逻辑在预定的扩展点调用同一 EventBus 触发事件，传入业务输入，以及根据服务端会话记录构造的 SessionContext。扩展为该事件注册 Handler 后，在业务逻辑触发事件时按契约参与处理。

**新增 Ops 能力。** 产品先实现连接所属服务的能力函数，再由 AgentServer 在 Registry 初始化前，通过产品注册函数为同一 EventBus 中的 `ops.build` 事件注册 BUILTIN 层的 EventHandler，即 Build Handler。产品注册和 Registry 初始化完成后，AgentServer 显式触发 `ops.build`；Build Handler 向同一个 Builder 注册能力的命名空间和方法，Events 校验后冻结并保存 Builder，构建成功后才加载扩展包。每次进入普通 Handler 时，使用冻结 Builder 创建独立 Ops 并绑定本次上下文，普通事件不再触发 `ops.build`。普通 Handler 通过 Ops 调用能力，能力函数在调用时执行校验和实际操作。构建协议以 [Events 公共契约](../../../../../agent-core/openjiuwen/core/events/docs/specs/S_01_events-contract.md#43-opsbuild)为准。

AgentServer、WebUI、Gateway 分别提供所属领域的服务与适配。增加组件或渠道后，还需接通事件或能力入口，才能供扩展使用；一个 Ops 方法与一个事件类型不要求一一对应。通用步骤和分领域例子见第 5.3 节。

## 3. 关键流程与完成语义

### 3.1 入口与正常链路

系统有三条相互关联、触发时机不同的链路。

**包管理链路：使扩展可用。** AgentServer 准备 EventBus，直接调用产品注册函数，注册产品事件及 Core Handler、BUILTIN Handler 和 Ops Build Handler，再将同一 EventBus 传给 `Registry.init`。Registry 只补充 Extension 管理事件及其 Core Handler。AgentServer 确认产品注册和管理初始化完成后，显式触发 `ops.build`，能力目录构建成功后才提交本地包路径。agent-core 加载包、记录注册并返回逐包管理结果（Report）；AgentServer 核对生效与残留后开放对应业务入口。

**业务调用链路：让扩展参与一次处理。** 业务逻辑在预定的扩展点调用 EventBus 触发事件，传入包含业务输入的 Event、根据服务端会话记录构造的 SessionContext，并保留原请求授权范围。EventBus 匹配本次 Handler，按 USER（个人扩展）、BUILTIN（平台内置）、CORE（Core Handler）层级推进；每次进入普通 Handler 前独立构建 Ops。Handler 可按契约调用 next 继续处理，也可直接返回；实际进入 Core Handler 时，由其调用所属服务执行默认动作。结果沿已进入的调用链返回触发事件的业务逻辑，不再补跑同一默认动作。例如，AgentServer 在回合准备步骤触发产品事件，处理继续到该事件的 Core Handler 时，再调用底层准备服务；具体类型由对应特性设计文档定义。

**UI界面链路：打开面板、交互和更新。** 用户选择可用的扩展UI界面项后，Web前端在兼容的 slot 中创建视图并申请挂载。AgentServer WebUI 服务校验请求，保存这个视图的归属和投递记录。扩展生成 JSON UI界面树，经校验并沿 AgentServer Front、Gateway 和 WebClient 的传输链路发送到对应视图，由Web前端显示。用户点击面板中的按钮时，服务端先根据视图绑定核对会话和权限，再触发允许的 UI 事件；生成的新画面仍发送到指定视图。后端也可在有效业务调用中主动更新指定视图，无需等待中的聊天请求。

包加载后可以处理多次业务调用；业务状态可先于视图存在，用户尚未打开面板时仍可保存业务结果，打开后再生成画面。没有UI界面的扩展只需接入前两条链路。详细协作见[架构第 5 节](extensions-v2-architecture.md#5-模块协作与处理流程)。

### 3.2 完成与交付的确认点

| 结果 | 由谁确认 | 对整体流程意味着什么 |
| --- | --- | --- |
| 初始化完成 | AgentServer 确认产品注册完成；Registry 确认同一 EventBus 上的管理组件及事件就绪 | 可以显式构建能力目录，尚未证明用户包生效或所有业务能力可用 |
| 能力目录构建成功 | AgentServer 显式触发 `ops.build`，Events 校验、冻结并保存 Builder | 可以提交包路径；普通事件具备创建 Ops 的前提，实际授权和服务可用性仍须检查 |
| 包可用 | AgentServer 核对 agent-core 生效记录与 Report | 允许对应入口使用扩展 |
| 事件返回 | 调用方取得符合契约的结果 | 本次调用结束；Core Handler 是否执行需另核对 |
| 业务完成 | 所属服务记录或外部回执 | 实际操作已完成，扩展后续处理与UI界面显示仍可能失败 |
| UI界面交付 | 传输确认发送，Web前端确认本地应用 | 发送和显示分别成立；服务端取得应用确认的协议尚待建立 |

初始化、绑定、渠道收发及卸载的完整确认依据见[架构第 6.3 节](extensions-v2-architecture.md#63-完成与结果交接)。

### 3.3 控制、失败与重试

失败由对应环节处理：AgentServer 核对加载与清理，业务方核对实际操作，WebUI 与传输处理内容和发送，Web前端处理本地应用。

产品注册失败时，AgentServer 停止后续装配并核对本次产品注册的清理；Registry 自身初始化失败时，只按 Extension 约定清理本次管理注册和自有资源，保留 JiuwenSwarm 已有注册。AgentServer 汇总错误和残留，确认清理完成后再重建相关实例，不在有残留的 EventBus 上盲目重试。

实际操作完成后，即使扩展后续处理或UI界面发送失败，已发生的业务结果仍然有效。超时或断线导致结果未知时，先查询实际结果，再决定是否重试；重新取得画面不会自动重放点击或工具操作。详细责任见[架构第 8 节](extensions-v2-architecture.md#8-故障处理与继续执行)。

## 4. 持续状态与关键约束

### 4.1 状态与资源生命周期

包实例和注册属于当前 AgentServer 运行实例；Ops 随所属 Handler 结束而失效。面板视图由Web前端创建和释放，对应的视图绑定记录由 AgentServer WebUI 服务管理。关闭面板、切换会话、Web前端断线或包退出时，相关记录失效，后续交互和画面更新不能继续使用旧记录；重新打开或重连时需按挂载协议建立新的有效关联。

每个 AgentServer 业务运行实例按一个 EventBus 对应一个 Registry 装配，业务、管理和 USER 注册使用同一 EventBus。AgentServer 协调 EventBus 的创建和最终释放，不在初始化后替换实例；多个独立 Registry 共用同一个 EventBus 不在本期范围内。

卸载或停机要先限制新使用，确认在途调用及相关能力使用结束后才释放资源。AgentServer 负责自身业务调用的跟踪与等待，Extension 负责包注册撤销和资源清理；Events 不提供调用跟踪或 drain（停止接收新调用并等待在途调用结束）。在线单包卸载仍需验证目标包的使用结束协作后开放。重载先完整卸载再安装，失败不自动恢复旧实例；卸载不自动删除用户源码和业务结果。退出顺序见[架构第 5.2.4 节](extensions-v2-architecture.md#524-视图退出包卸载重载与-agentserver-停止)。

### 4.2 恢复与一致性

包记录、业务结果和Web前端画面分别生效，没有跨模块事务。重启可以重新加载包，重连可以重新认证并建立视图、取得当前完整树，但都不能续接旧调用栈或重放历史交互。版本、并发更新和页面应用确认仍待定稿，恢复限制见[架构第 8.3 节](extensions-v2-architecture.md#83-恢复依据与继续边界)。

### 4.3 授权、隔离与资源约束

AgentServer 与 Gateway 检查接入范围，业务服务在实际操作前检查当前权限；会话或包标识本身不授予权限。UI界面操作通过当前有效的视图绑定记录核对具体面板及其归属，知道记录标识也不能代替权限检查。目标面板已关闭或记录已失效时，停止这次交互或更新，不改发到其他面板。

Web前端只呈现平台允许的组件、动作和样式。独立 Ops 不会自动隔离扩展全局状态，受限接口也不能代替不可信 Python 代码的沙箱。并发、嵌套、UI界面树和发送积压需设限并实测，当前没有数值保证。检查位置与隔离责任见[架构第 9 节](extensions-v2-architecture.md#9-权限隔离与资源限制)。

## 5. 现有基础与落地边界

### 5.1 可复用基础与待建设能力

截至本次修订，agent-core Events 已有公共模型、Protocol、公开导出和契约测试文件，可作为产品适配的接口基础；EventBus、Builder 和执行链运行时尚未实现。`extensions_v2` 与 agent-core Extension 仍只有规则和设计材料，尚无本方案运行实现或 V2 集成验收记录。源码及测试入口见[架构第 3.4 节](extensions-v2-architecture.md#34-已有基础与接入缺口)。

旧扩展启停、工具与会话服务、Web 代理、主动推送和渠道收发提供了接入基础。V2 仍需建设公共机制、业务扩展点的事件触发、可信上下文、扩展UI界面项与绑定、树渲染和跨服务适配。源码依据与缺口集中在[架构第 3.4 节](extensions-v2-architecture.md#34-已有基础与接入缺口)，旧机制的逐类迁移约束见[第 10.3 节](extensions-v2-architecture.md#103-保留删除升级与迁移)。

### 5.2 能力开放条件

能力按范围分别开放：基础扩展需先证明加载结果可核对、业务逻辑在预定扩展点触发事件并使用处理结果、可信包身份到 Ops 的适配及会话和授权正确；WebUI 还需完成UI界面项挂载、交互、定向推送及重连的真实联调；在线管理、渠道接入和旧扩展迁移各自验证生命周期与兼容边界。

架构[第 10.1 节](extensions-v2-architecture.md#101-保证范围与成立条件)集中维护各项能力的具体开放条件，[第 11.2 节](extensions-v2-architecture.md#112-架构要求与验证归属)说明验证主题与责任。未满足条件时限制对应入口；当前没有 V2 验收记录，旧测试通过不能替代 V2 验收。

<a id="34-ops-与事件的接入示例"></a>

### 5.3 详细设计与实施文档索引

| 阅读目的 | 入口 |
| --- | --- |
| 理解模块组成和内部工作方式 | 架构[第 3 节](extensions-v2-architecture.md#3-架构总览与模块分工)、[第 4 节](extensions-v2-architecture.md#4-关键模块设计) |
| 开放产品能力、增加扩展参与点 | [Ops 接入](extensions-v2-architecture.md#43-ops-能力注册与服务适配)、[事件类型接入](extensions-v2-architecture.md#42-业务事件定义与扩展点接入) |
| 查阅 AgentServer、WebUI、Gateway 接入例子 | [新增 Ops 示例](extensions-v2-architecture.md#114-新增-ops-的具体例子)、[新增事件示例](extensions-v2-architecture.md#115-新增事件类型的具体例子) |
| 核对协作流程、完成依据与数据归属 | 架构[第 5 节](extensions-v2-architecture.md#5-模块协作与处理流程)、[第 6 节](extensions-v2-architecture.md#6-状态事件与完成条件)、[第 7 节](extensions-v2-architecture.md#7-数据管理与保存规则) |
| 查找前提、待确认的设计内容和参考来源 | 架构[第 12.2 节](extensions-v2-architecture.md#122-未验证前提与待确认问题)、[第 12.3 节](extensions-v2-architecture.md#123-参考依据与文档分工) |

正式规约（spec）、特性方案（feature）和统一开发计划尚未建立。后续按[模块规范](../../AGENTS.md)分别维护定稿契约、实施与验证证据、任务与进度；概览同步整体结论。
