# JiuwenSwarm 内置事件开发模板

本文面向 WebClient、Gateway 和 AgentServer 开发者。目录以当前实现为准，新增示例文件另行标注；管理、事件和 WebUI 类型使用真实接口，业务处理用带注释的伪代码表示。本文只指导 swarm 内置事件开发。最近修订：2026-10-22。

## 1. 先看整体流程

### 1.1 事件在哪里执行

三类开发者提供的事件和 Python Handler 都在 AgentServer 中执行；WebClient 和 Gateway 负责采集、转发或执行所属模块的动作。

```mermaid
flowchart LR
    FE["React 前端"] --> WC["WebClient"]
    WC --> GW["Gateway"]
    GW --> AD["AgentServer 适配器<br/>请求转为 Event"]
    AD --> BUS["同一个 EventBus"]
    BIZ["AgentServer 本地业务"] --> BUS
    BUS --> H["匹配的 Handler<br/>USER → BUILTIN → CORE"]
```

### 1.2 文件放在哪里，登记后怎样生效

三类目录中的文件提供登记函数，`bootstrap.py` 汇总这些登记，`assembly.py` 将汇总函数作为宿主回调传入初始化。core 调用回调后完成一次能力构建；业务发生时才执行普通 Handler。

```mermaid
sequenceDiagram
    participant AS as AgentServer 启动
    participant A as assembly.py
    participant M as ExtensionManager
    participant R as core Registry
    participant S as bootstrap.py
    participant D as 内置事件与 Handler 目录
    participant B as EventBus

    AS->>A: install_extensions(server)
    A->>B: 创建 ExtensionEventBus
    Note over B: core 内置事件已登记
    A->>M: init(bus, 两类宿主回调)
    M->>R: init(bus, 两类宿主回调)
    R->>A: host_event_types(bus)
    A->>S: register_event_types(bus, webui)
    S->>D: event_types/ 登记（WebUI 经 WebUIRuntime）
    D->>B: register_event_type(名称, 匹配函数, CORE)
    R->>A: host_handlers(bus)
    A->>S: register_handlers(bus, webui)
    opt 新增普通 BUILTIN Handler（当前目录预留）
        S->>D: event_handlers/ 中的登记函数
        D->>B: register_event_handler(..., BUILTIN)
    end
    S->>D: handler_ops/ 登记（当前经 WebUIRuntime）
    D->>B: 为 ops.build 登记 BUILTIN Handler
    R->>B: trigger(Event("ops.build", None))
    B->>D: 执行 Ops Build Handler
    Note over D: 通过 next 取得同一个 Builder<br/>添加 namespace 和 method 后返回
    D-->>B: 返回 Builder
    B->>B: 保存能力目录
    B-->>R: Build 成功
    R-->>M: core 初始化完成
    M->>R: open_install_dir(解析后的路径)
    M-->>A: init 返回
    A->>M: load_installed()
    M-->>A: 返回逐包结果
    A-->>AS: 返回装配结果
```

### 1.3 触发后按什么顺序执行

下图画出处理继续到 CORE 的分支。匹配的 USER、BUILTIN Handler 可以按契约短路；同层按登记顺序执行。CORE 负责默认动作，不再调用 next。

```mermaid
sequenceDiagram
    participant P as 业务入口 / 请求适配器
    participant B as ExtensionEventBus
    participant U as 匹配的 USER Handler
    participant H as 匹配的 BUILTIN Handler
    participant C as 事件 CORE
    participant UI as ui.render 事件链
    participant T as RenderTransport

    P->>B: trigger(Event(event_type, input), session)
    B->>U: 调用 Handler
    U->>H: await next_handler(event)
    H->>C: await next_handler(event)
    C->>C: 校验输入，执行默认处理
    opt 默认处理需要更新页面
        C->>B: trigger(Event("ui.render", request), session)
        B->>UI: 执行新的渲染事件链
        UI->>T: 校验与生成帧后发送
        T-->>UI: 发送结果
        UI-->>B: UiRenderOutput
        B-->>C: 渲染结果
    end
    C-->>H: 业务结果
    H-->>U: 处理结果
    U-->>B: 处理结果
    B-->>P: 本次事件结果
```

`next_handler` 推进当前链；`trigger` 或动词 Ops 能力发起新的事件链。事件返回后，入口不要再次执行已经由 CORE 完成的动作。

匹配结果在进入执行链前已经固定，Handler 替换 `event_input` 后不会重新匹配；多次调用 `next_handler` 会重复执行下游处理。Handler、CORE 或 Ops 抛出的异常会向外传播，由业务入口决定如何处理；EventBus 不自动重试，也不撤销已发生的操作。

## 2. 三个模块共用的目录和启动方式

### 2.1 代码放在哪里

下面列出现有目录和主要文件；标为“新增示例”的文件是本模板的开发落点，尚需实现。

```text
jiuwenswarm/extensions_v2/
├── __init__.py
├── assembly.py                         # 创建运行实例，接入唯一一次初始化
├── bootstrap.py                        # 汇总事件、普通 Handler 与 Ops 登记
├── config.py                           # 解析安装目录
├── event_types/                        # 内置事件名、输入输出、匹配及类型登记
│   ├── __init__.py
│   ├── webui.py                        # 已有：WebUI 事件登记，第 3 章在此扩充
│   ├── context_usage.py                # 已有：用量事件
│   ├── matching.py                     # 已有：匹配辅助函数
│   ├── channel_event.py                # 新增示例
│   └── agent_event.py                  # 新增示例
├── event_handlers/                     # 内置普通 BUILTIN Handler；当前仅预留目录
│   ├── __init__.py
│   ├── webui_handler.py                # 新增示例
│   ├── channel_handler.py              # 新增示例
│   └── agent_handler.py                # 新增示例
├── handler_ops/                        # 内置 Ops 能力、ops.build Handler 与登记
│   ├── __init__.py
│   ├── ui.py                           # 已有：ui 能力，第 3 章在此扩充
│   ├── channel_ops.py                  # 新增示例
│   └── agent_ops.py                    # 新增示例
├── management/
│   ├── __init__.py
│   └── registry.py                     # ExtensionManager，委托 core Registry
└── webui/                              # WebUI 适配器、模型、渲染与传输服务
    ├── adapter.py
    ├── models.py
    ├── runtime.py
    └── core_handlers/                  # 依赖 WebUI 运行时的 CORE
        ├── __init__.py
        ├── action_core.py              # 已有：上行 UI 默认处理
        ├── render_core.py              # 已有：渲染默认处理
        └── custom_core.py              # 新增示例：本章自定义事件的 CORE
```

CORE 随类型一起登记；通用 CORE 可与事件定义同文件，依赖 WebUI 运行时的 CORE 放在 `webui/core_handlers/`。`event_handlers/` 放额外的 BUILTIN Handler，`handler_ops/` 按能力名称分文件，所有能力仍在 AgentServer 中登记和执行。

### 2.2 启动时登记事件、Handler 和 Ops 能力

当前 `assembly.py` 已把 `bootstrap.register_event_types`、`bootstrap.register_handlers` 包装成两类宿主回调。第 3～5 章的新登记加到 `bootstrap.py`。**新增文件必须显式接入，不会因为放进目录就自动生效。**

```python
from openjiuwen.core.events import ExtensionEventBus
from openjiuwen.core.extension import ActivationReport

from jiuwenswarm.extensions_v2 import bootstrap
from jiuwenswarm.extensions_v2.assembly import ExtensionAssembly


# assembly.py：摘出已有入口的登记关系，不新增第二次初始化。
async def initialize_builtin(
    assembly: ExtensionAssembly,
    *,
    host_version: str | None,
) -> list[ActivationReport]:
    def register_event_types(bus: ExtensionEventBus) -> None:
        bootstrap.register_event_types(bus, assembly.webui)

    def register_handlers(bus: ExtensionEventBus) -> None:
        bootstrap.register_handlers(bus, assembly.webui)

    await assembly.manager.init(
        assembly.bus,
        host_version=host_version,
        host_event_types=register_event_types,
        host_handlers=register_handlers,
    )
    # 门面委托 Registry.init 执行回调和唯一一次 ops.build，再设置安装目录。
    # 保留生产入口已有的单例、失败处理和后续步骤，不再手工触发 Build。
    return await assembly.manager.load_installed()
```

安装目录仍由 `resolve_install_dir()` 解析，默认 `~/.jiuwenswarm/extension_pkgs`；core 只接收路径。接入新登记时，同一事件或 Ops namespace 只登记一次。

首次 Build 成功后不再增删 Build Handler，内置能力调整通过重启重新装配。

## 3. WebClient 开发模板：界面交互

本章的 `ui.custom` 是新事件占位名。以下声明按文件拆分；已有 WebUI 模型与接口直接复用，注释处补充业务实现。

### 3.1 文件与事件契约

| 文件 | 本章要提供的内容 |
| --- | --- |
| `event_types/webui.py` | 事件名、输入输出模型引用、匹配函数、类型登记 |
| `webui/core_handlers/custom_core.py` | 新增示例：依赖 WebUI 运行时的 CORE 工厂 |
| `event_handlers/webui_handler.py` | BUILTIN Handler、Handler 登记 |
| `handler_ops/ui.py` | 需要从其它 Handler 主动触发时，在已有 ui 能力中增加方法 |
| `webui/constants.py` | 将新事件加入 `WebUIEvent` 和 `UPSTREAM_EVENTS` |
| `webui/adapter.py` 与前端 | 请求校验、事件转换、交互上报和结果返回 |

### 3.2 定义事件名、输入和输出

```python
# event_types/webui.py：在已有文件中扩充。
from __future__ import annotations

from typing import TYPE_CHECKING
from openjiuwen.core.events import EventType
from jiuwenswarm.extensions_v2.webui.models import UiActionInput, UiActionOutput

UI_EVENT_TYPE: EventType = "ui.custom"  # 替换为确认后的新事件名，同步加入上行白名单。

# 复用真实输入 UiActionInput，不重新定义同名类：
#   event_type: str
#   target: RenderTarget，含 session_id、slot_id、package_id、instance_key
#   payload: Mapping[str, Any]，本事件需要的字段及校验规则由开发者确定。
#
# 复用真实输出 UiActionOutput：
#   accepted: bool，status: str，code: str | None
# 以下 CORE、BUILTIN、Ops 和触发入口都使用这两个模型。
```

### 3.3 定义匹配函数、CORE 和类型登记

```python
# webui/core_handlers/custom_core.py：新增示例，依赖 WebUI 运行时的 CORE。
from __future__ import annotations

from typing import TYPE_CHECKING
from openjiuwen.core.events import Event, EventHandler, EventHandlerOps, EventNext
from jiuwenswarm.extensions_v2.webui.models import UiActionInput, UiActionOutput

if TYPE_CHECKING:
    from jiuwenswarm.extensions_v2.webui.runtime import WebUIRuntime


def build_webui_core(runtime: WebUIRuntime) -> EventHandler:
    async def ui_core(
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> UiActionOutput:
        request = event.event_input
        if not isinstance(request, UiActionInput):
            raise TypeError("UI event requires UiActionInput")
        # 按事件契约校验目标与参数，借助 runtime 中的服务执行默认处理。
        # 返回 UiActionOutput；CORE 不调用 next_handler。
        # 需要所有 Handler 执行前完成的校验，应放到请求适配器。
        raise NotImplementedError("实现本事件的默认处理")

    return ui_core
```

```python
# event_types/webui.py：接上节的常量与模型，登记仍留在事件定义模块。
from openjiuwen.core.events import ExtensionEventBus, MatchPattern
from jiuwenswarm.extensions_v2.webui.core_handlers.custom_core import build_webui_core

if TYPE_CHECKING:
    from jiuwenswarm.extensions_v2.webui.runtime import WebUIRuntime


def match_webui_event(event_input: object, match_pattern: MatchPattern) -> bool:
    if not isinstance(event_input, UiActionInput):
        return False
    # 比较声明的匹配字段，例如 target.slot_id、payload.action_id；空模式全匹配。
    # 只选择 Handler，不执行请求校验、网络调用或业务操作。
    raise NotImplementedError("实现 UI 输入字段匹配")


def register(bus: ExtensionEventBus, runtime: WebUIRuntime) -> None:
    bus.register_event_type(UI_EVENT_TYPE, match_webui_event, build_webui_core(runtime))
    # register 是本章新增的汇总入口，这里只展示新增类型；保留原有 ui.render 等登记。
    # 新事件同步加入 WebUIEvent、UPSTREAM_EVENTS 和前端类型。
    # 若原逻辑遍历白名单注册，改为在同一处选择本事件的 CORE，不重复登记。
```

### 3.4 定义 BUILTIN Handler 和登记入口

```python
# event_handlers/webui_handler.py
from openjiuwen.core.events import (
    Event, EventHandlerOps, EventNext, ExtensionEventBus, HandlerLevel,
)
from jiuwenswarm.extensions_v2.event_types.webui import UI_EVENT_TYPE
from jiuwenswarm.extensions_v2.webui.models import UiActionInput, UiActionOutput


async def on_webui_event(
    event: Event,
    ops: EventHandlerOps | None,
    next_handler: EventNext,
) -> UiActionOutput:
    if not isinstance(event.event_input, UiActionInput):
        raise TypeError("UI event requires UiActionInput")
    # 在这里增加前置处理；按契约短路时也必须返回 UiActionOutput。
    output = await next_handler(event)
    if not isinstance(output, UiActionOutput):
        raise TypeError("UI event must return UiActionOutput")
    # 在这里记录或加工结果；Ops 与 next 只在本次 Handler 调用中使用。
    return output


def register(bus: ExtensionEventBus) -> None:
    bus.register_event_handler(UI_EVENT_TYPE, on_webui_event, {}, HandlerLevel.BUILTIN)
    # {} 表示全部接收；按需改为本事件匹配函数支持的字段模式。
```

### 3.5 定义 Ops 能力和 Build Handler

前端上报无需新增 Ops。若还需要从其它 Handler 触发此事件，可在现有 ui Build Handler 中增加 `dispatch`；下面同时保留已有 `render`，避免创建第二个 ui namespace。

```python
# handler_ops/ui.py：在已有工厂中补充能力，不另建一套 ui Build Handler。
from openjiuwen.core.events import (
    OPS_BUILD_EVENT_TYPE, Event, EventHandler, EventHandlerOps,
    EventHandlerOpsBuilder, EventNext, ExtensionEventBus, HandlerLevel,
)
from jiuwenswarm.extensions_v2.event_types.webui import UI_EVENT_TYPE
from jiuwenswarm.extensions_v2.webui.constants import WebUIEvent
from jiuwenswarm.extensions_v2.webui.models import (
    UiActionInput, UiActionOutput, UiRenderInput,
)


def build_webui_ops_build_handler(bus: ExtensionEventBus, render_event_type: str) -> EventHandler:
    async def ui_render(ops_view: EventHandlerOps, request: UiRenderInput) -> object:
        # 保留已有能力：使用当前会话，发起新的 ui.render 事件链。
        return await bus.trigger(Event(render_event_type, request), ops_view.session_context)

    async def ui_dispatch(ops_view: EventHandlerOps, request: UiActionInput) -> UiActionOutput:
        # 先校验会话、目标和 request.event_type 与本事件是否一致。
        output = await bus.trigger(Event(UI_EVENT_TYPE, request), ops_view.session_context)
        if not isinstance(output, UiActionOutput):
            raise TypeError("UI event must return UiActionOutput")
        return output

    async def ui_ops_build(
        event: Event, ops: EventHandlerOps | None, next_handler: EventNext,
    ) -> EventHandlerOpsBuilder:
        builder = await next_handler(event)
        if not isinstance(builder, EventHandlerOpsBuilder):
            raise TypeError("ops.build must return EventHandlerOpsBuilder")
        builder.add_namespace("ui")
        builder.add_method("ui", "render", ui_render)
        builder.add_method("ui", "dispatch", ui_dispatch)
        return builder  # 同一个 Builder；此时不渲染，也不执行新事件。

    return ui_ops_build


def register(bus: ExtensionEventBus) -> None:
    bus.register_event_handler(
        OPS_BUILD_EVENT_TYPE,
        build_webui_ops_build_handler(bus, WebUIEvent.RENDER.value),
        {},
        HandlerLevel.BUILTIN,
    )
```

### 3.6 接入启动登记

新增登记加到现有 `bootstrap.py`，保留其它领域条目。`assembly.py` 已将下面两个汇总函数作为回调传入唯一一次初始化，Registry 在回调返回后完成 Build，不另建初始化入口。

```python
# bootstrap.py：本章新增内容的接入示意。
from openjiuwen.core.events import ExtensionEventBus
from jiuwenswarm.extensions_v2.event_types.context_usage import register_context_usage_events
from jiuwenswarm.extensions_v2.event_types import webui as webui_types
from jiuwenswarm.extensions_v2.event_handlers import webui_handler
from jiuwenswarm.extensions_v2.handler_ops import ui as ui_ops
from jiuwenswarm.extensions_v2.webui.runtime import WebUIRuntime


def register_event_types(bus: ExtensionEventBus, webui: WebUIRuntime) -> None:
    register_context_usage_events(bus)  # 保留现有用量事件。
    webui_types.register(bus, webui)    # 新汇总入口保留原有及新增 UI 类型。
    # 采用本入口后，不再额外调用 webui.register_event_types 重复登记同一类型。


def register_handlers(bus: ExtensionEventBus, webui: WebUIRuntime) -> None:
    webui_handler.register(bus)
    ui_ops.register(bus)  # 保留 render、按需增加 dispatch；替代原 webui.register_handlers。
    # 普通 Handler 和 Ops Build Handler 都要登记，保留其它领域条目。
```

### 3.7 接通触发入口与结果返回

前端同步扩充事件类型，并在实际交互位置调用上报函数：

```typescript
// jiuwenswarm/channels/web/frontend/src/features/webui/types.ts：扩充原有类型。
export type WebUIActionEvent =
  | 'ui.ready' | 'ui.press' | 'ui.toggle' | 'ui.input' | 'ui.select' | 'ui.custom';
```

```typescript
// jiuwenswarm/channels/web/frontend/src/features/webui/customEvent.ts：新增示例。
import { dispatchWebUIAction } from './actionBridge';
import type { WebUIRenderTarget } from './types';

export async function reportUiEvent(
  target: WebUIRenderTarget,
  payload: Record<string, unknown>,
): Promise<void> {
  // 在交互采集点准备本事件的 payload，使用当前视图的完整 target。
  await dispatchWebUIAction('ui.custom', target, payload);
  // 现有 bridge 返回 void；如需展示 accepted/status，应扩充它的回执处理。
}
```

WebClient、Gateway 继续转发 `webui.dispatch`；核对 Gateway 的 [webui_handlers.py](../../../gateway/channel_manager/web/webui_handlers.py) 已覆盖新事件。真正把请求变成事件的是 AgentServer 的 `WebUIDispatchAdapter`（设计材料中的 `WebUIEventAdapter`）。

```python
# webui/adapter.py：已有适配器的方法骨架；下面的转换函数需按业务补齐。
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions_v2.assembly import get_webui_runtime
from jiuwenswarm.extensions_v2.webui.models import UiActionInput, UiActionOutput
from jiuwenswarm.server.runtime.gateway_adapter.base import GatewayAdapter, build_error_response


def parse_ui_request(request: AgentRequest) -> UiActionInput:
    # 校验事件白名单、会话、target 和本事件的 payload，构造 RenderTarget 与 UiActionInput。
    # 目标和身份按服务端记录核对，不能只采信 payload。
    raise NotImplementedError("实现请求校验与输入转换")


def build_ui_response(request: AgentRequest, output: UiActionOutput | None) -> AgentResponse:
    # 关联原 request_id，把 accepted、status、code 转成响应；None 不作为成功。
    raise NotImplementedError("实现 UI 回执转换")


class WebUIDispatchAdapter(GatewayAdapter):
    methods: frozenset[str] = frozenset({ReqMethod.WEBUI_DISPATCH.value})

    async def handle(self, request: AgentRequest) -> AgentResponse:
        runtime = get_webui_runtime()
        if runtime is None:
            return build_error_response(request, "WebUI unavailable")
        action = parse_ui_request(request)
        output = await runtime.dispatch(
            action, user_id=request.user_id, channel_id=request.channel_id or "",
        )
        # runtime.dispatch 构造 SessionContext，并调用同一个 EventBus.trigger。
        # 合入现有适配器时，保留其异常日志与错误响应转换逻辑。
        return build_ui_response(request, output)
```

该适配器已由 AgentServer 注册表接入，复用已有实例。需要更新画面时，Handler 使用 `ops.ui.render(UiRenderInput(...))`，渲染结果是 `UiRenderOutput`；交互回执与渲染推送分别处理。

### 3.8 完成后检查

单元测试覆盖前端类型和白名单、类型唯一登记、匹配、继续与短路、非法输入、失效目标、Ops Build、请求回执与渲染失败。最后通过真实 WebClient/Gateway 链路验证，不能仅凭推送入口返回成功认定页面已显示。

## 4. Gateway 开发模板：请求转发与渠道事件

本章的 `channel.custom` 是新事件占位名；输入输出是下文新定义的业务模型，不是已有 swarm API。业务文件按以下声明补齐，具体操作在注释处实现。

### 4.1 文件与事件契约

| 文件 | 本章要提供的内容 |
| --- | --- |
| `event_types/channel_event.py` | 事件名、输入输出、匹配函数、CORE 工厂、类型登记 |
| `event_handlers/channel_handler.py` | BUILTIN Handler、Handler 登记 |
| `handler_ops/channel_ops.py` | 可选的主动触发能力、Build Handler、能力登记 |
| 所属业务入口 | 准备可信会话、触发事件、转换并使用结果 |

### 4.2 定义事件名、输入和输出

```python
# event_types/channel_event.py
from collections.abc import Mapping
from dataclasses import dataclass, field
from openjiuwen.core.events import EventType

CHANNEL_EVENT_TYPE: EventType = "channel.custom"  # 替换为确认后的事件名；字段按实际业务调整。


@dataclass(frozen=True, slots=True)
class ChannelEventInput:
    channel_id: str
    target_id: str
    action: str
    payload: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ChannelEventOutput:
    accepted: bool
    status: str
    data: Mapping[str, object] = field(default_factory=dict)
    # 由事件契约明确 accepted、status 及 data 的含义，调用方据此继续业务。
```

### 4.3 定义匹配函数、CORE 和类型登记

```python
# event_types/channel_event.py：接上节的常量与模型。
from openjiuwen.core.events import (
    Event, EventHandler, EventHandlerOps, EventNext, ExtensionEventBus, MatchPattern,
)


def match_channel_event(event_input: object, match_pattern: MatchPattern) -> bool:
    if not isinstance(event_input, ChannelEventInput):
        return False
    # 比较 channel_id、target_id、action 等约定字段；空模式全匹配，不执行业务。
    raise NotImplementedError("实现本事件输入字段匹配")


def build_channel_core() -> EventHandler:
    async def channel_core(
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> ChannelEventOutput:
        request = event.event_input
        if not isinstance(request, ChannelEventInput):
            raise TypeError("channel.custom requires ChannelEventInput")
        if ops is None or ops.session_context is None:
            raise ValueError("channel.custom requires SessionContext")
        # 检查会话是否允许该渠道动作，通过 AgentServer 侧受控代理交给 Gateway，并映射执行结果。
        # 返回 ChannelEventOutput；CORE 不调用 next_handler，入口不补做同一动作。
        raise NotImplementedError("实现本事件的默认处理")

    return channel_core


def register(bus: ExtensionEventBus) -> None:
    bus.register_event_type(CHANNEL_EVENT_TYPE, match_channel_event, build_channel_core())
    # 原有本领域事件迁入后也在此汇总，每个类型只登记一次。
```

### 4.4 定义 BUILTIN Handler 和登记入口

```python
# event_handlers/channel_handler.py
from openjiuwen.core.events import (
    Event, EventHandlerOps, EventNext, ExtensionEventBus, HandlerLevel,
)
from jiuwenswarm.extensions_v2.event_types.channel_event import (
    CHANNEL_EVENT_TYPE, ChannelEventInput, ChannelEventOutput,
)


async def on_channel_event(
    event: Event,
    ops: EventHandlerOps | None,
    next_handler: EventNext,
) -> ChannelEventOutput:
    if not isinstance(event.event_input, ChannelEventInput):
        raise TypeError("channel.custom requires ChannelEventInput")
    # 前置检查或加工写在这里；短路也必须返回 ChannelEventOutput。
    output = await next_handler(event)
    if not isinstance(output, ChannelEventOutput):
        raise TypeError("channel.custom must return ChannelEventOutput")
    # 后置处理写在这里；Ops 与 next 不保存到本次调用之外使用。
    return output


def register(bus: ExtensionEventBus) -> None:
    bus.register_event_handler(CHANNEL_EVENT_TYPE, on_channel_event, {}, HandlerLevel.BUILTIN)
    # 按需把 {} 改为匹配函数支持的字段模式，不能用此入口覆盖 CORE。
```

### 4.5 定义 Ops 能力和 Build Handler

需要其它 Handler 主动触发时增加 `ops.channel.dispatch`；只有业务入口触发时可以不增加这一能力。

```python
# handler_ops/channel_ops.py
from openjiuwen.core.events import (
    OPS_BUILD_EVENT_TYPE, Event, EventHandler, EventHandlerOps,
    EventHandlerOpsBuilder, EventNext, ExtensionEventBus, HandlerLevel,
)
from jiuwenswarm.extensions_v2.event_types.channel_event import (
    CHANNEL_EVENT_TYPE, ChannelEventInput, ChannelEventOutput,
)


def build_channel_ops_handler(bus: ExtensionEventBus) -> EventHandler:
    async def dispatch(ops_view: EventHandlerOps, request: ChannelEventInput) -> ChannelEventOutput:
        if ops_view.session_context is None:
            raise ValueError("ops.channel.dispatch requires SessionContext")
        # 调用能力时校验目标和允许范围，使用当前会话发起一条新事件链。
        output = await bus.trigger(Event(CHANNEL_EVENT_TYPE, request), ops_view.session_context)
        if not isinstance(output, ChannelEventOutput):
            raise TypeError("channel.custom must return ChannelEventOutput")
        return output

    async def channel_ops_build(
        event: Event, ops: EventHandlerOps | None, next_handler: EventNext,
    ) -> EventHandlerOpsBuilder:
        builder = await next_handler(event)
        if not isinstance(builder, EventHandlerOpsBuilder):
            raise TypeError("ops.build must return EventHandlerOpsBuilder")
        builder.add_namespace("channel")
        builder.add_method("channel", "dispatch", dispatch)
        return builder  # 只登记函数，不执行业务；返回取得的同一个 Builder。

    return channel_ops_build


def register(bus: ExtensionEventBus) -> None:
    bus.register_event_handler(
        OPS_BUILD_EVENT_TYPE, build_channel_ops_handler(bus), {}, HandlerLevel.BUILTIN,
    )
    # namespace 已有其它能力时合并进原主责 Build Handler，不再创建第二个。
```

### 4.6 接入启动登记

新增登记加到现有 `bootstrap.py`，保留其它领域条目。`assembly.py` 已将下面两个汇总函数作为回调传入唯一一次初始化，Registry 在回调返回后完成 Build，不另建初始化入口。

```python
# bootstrap.py：本章新增内容的接入示意。
from openjiuwen.core.events import ExtensionEventBus
from jiuwenswarm.extensions_v2.event_types.context_usage import register_context_usage_events
from jiuwenswarm.extensions_v2.event_types import channel_event
from jiuwenswarm.extensions_v2.event_handlers import channel_handler
from jiuwenswarm.extensions_v2.handler_ops import channel_ops
from jiuwenswarm.extensions_v2.webui.runtime import WebUIRuntime


def register_event_types(bus: ExtensionEventBus, webui: WebUIRuntime) -> None:
    webui.register_event_types(bus)      # 保留现有 UI 事件。
    register_context_usage_events(bus)  # 保留现有用量事件。
    channel_event.register(bus)


def register_handlers(bus: ExtensionEventBus, webui: WebUIRuntime) -> None:
    webui.register_handlers(bus)  # 保留现有 ui Ops Build Handler。
    channel_handler.register(bus)
    channel_ops.register(bus)  # 若不提供此能力，则不加入这条登记。
```

### 4.7 接通触发入口与结果返回

Gateway 网络 Handler 负责路由，事件 Handler 在 AgentServer 执行。上行经 E2A 转为下列 AgentRequest；需要渠道动作时，CORE 再经受控代理调用 Gateway，并等待结果。E2A 是两进程间的请求协议。

```python
# AgentServer 侧：新业务适配器骨架，不表示已有通用渠道代理。
from openjiuwen.core.events import Event, ExtensionEventBus, SessionContext
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.extensions_v2.event_types.channel_event import (
    CHANNEL_EVENT_TYPE, ChannelEventInput, ChannelEventOutput,
)
from jiuwenswarm.server.runtime.gateway_adapter.base import AdapterRegistry, GatewayAdapter


def parse_channel_request(request: AgentRequest) -> tuple[ChannelEventInput, SessionContext]:
    # 校验 params、渠道和目标，构造 ChannelEventInput。
    # 根据服务端身份与会话记录准备 SessionContext，不采信 payload 自报身份。
    raise NotImplementedError("实现渠道请求和会话转换")


def build_channel_response(request: AgentRequest, output: ChannelEventOutput) -> AgentResponse:
    # 关联原 request_id，将 accepted、status、data 转为网络响应。
    raise NotImplementedError("实现渠道事件回执转换")


class ChannelEventAdapter(GatewayAdapter):
    def __init__(self, bus: ExtensionEventBus, method: ReqMethod) -> None:
        self.bus = bus
        self.methods = frozenset({method.value})  # 网络方法名，与 event_type 分开确定。

    async def handle(self, request: AgentRequest) -> AgentResponse:
        event_input, session = parse_channel_request(request)
        output = await self.bus.trigger(Event(CHANNEL_EVENT_TYPE, event_input), session)
        if not isinstance(output, ChannelEventOutput):
            raise TypeError("channel.custom must return ChannelEventOutput")
        return build_channel_response(request, output)


def register_channel_adapter(
    registry: AdapterRegistry, bus: ExtensionEventBus, method: ReqMethod,
) -> None:
    registry.register(ChannelEventAdapter(bus, method))
    # Gateway 侧用同一个 ReqMethod 登记路由，按现有 proxy_unary_request 完成转发。
    # 新方法需要先补充 ReqMethod 及两端协议，不虚构一个当前不存在的枚举值。
```

### 4.8 完成后检查

单元测试覆盖类型唯一登记、匹配、继续与短路、目标拒绝、Ops Build、超时和执行结果。另行核对事件与网络方法的区别、Gateway 路由及结果回传，并完成跨进程联调；当前没有可直接复用的通用渠道事件代理。

## 5. AgentServer 开发模板：本地业务事件

<a id="agentserver-template"></a>

本章的 `agent.custom` 是新事件占位名；输入输出是下文新定义的业务模型，不是已有 swarm API。业务文件按以下声明补齐，具体操作在注释处实现。

### 5.1 文件与事件契约

| 文件 | 本章要提供的内容 |
| --- | --- |
| `event_types/agent_event.py` | 事件名、输入输出、匹配函数、CORE 工厂、类型登记 |
| `event_handlers/agent_handler.py` | BUILTIN Handler、Handler 登记 |
| `handler_ops/agent_ops.py` | 可选的主动触发能力、Build Handler、能力登记 |
| 所属业务入口 | 准备可信会话、触发事件、转换并使用结果 |

### 5.2 定义事件名、输入和输出

```python
# event_types/agent_event.py
from collections.abc import Mapping
from dataclasses import dataclass, field
from openjiuwen.core.events import EventType

AGENT_EVENT_TYPE: EventType = "agent.custom"  # 替换为确认后的事件名；字段按实际业务调整。


@dataclass(frozen=True, slots=True)
class AgentEventInput:
    agent_id: str
    step: str
    payload: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class AgentEventOutput:
    accepted: bool
    status: str
    data: Mapping[str, object] = field(default_factory=dict)
    # 由事件契约明确 accepted、status 及 data 的含义，调用方据此继续业务。
```

### 5.3 定义匹配函数、CORE 和类型登记

```python
# event_types/agent_event.py：接上节的常量与模型。
from openjiuwen.core.events import (
    Event, EventHandler, EventHandlerOps, EventNext, ExtensionEventBus, MatchPattern,
)


def match_agent_event(event_input: object, match_pattern: MatchPattern) -> bool:
    if not isinstance(event_input, AgentEventInput):
        return False
    # 比较 agent_id、step 等约定字段；空模式全匹配，不执行业务。
    raise NotImplementedError("实现本事件输入字段匹配")


def build_agent_core() -> EventHandler:
    async def agent_core(
        event: Event,
        ops: EventHandlerOps | None,
        next_handler: EventNext,
    ) -> AgentEventOutput:
        request = event.event_input
        if not isinstance(request, AgentEventInput):
            raise TypeError("agent.custom requires AgentEventInput")
        if ops is None or ops.session_context is None:
            raise ValueError("agent.custom requires SessionContext")
        # 检查当前 Agent 和业务阶段，调用本进程的所属业务服务，并转换默认处理结果。
        # 返回 AgentEventOutput；CORE 不调用 next_handler，入口不补做同一动作。
        raise NotImplementedError("实现本事件的默认处理")

    return agent_core


def register(bus: ExtensionEventBus) -> None:
    bus.register_event_type(AGENT_EVENT_TYPE, match_agent_event, build_agent_core())
    # 原有本领域事件迁入后也在此汇总，每个类型只登记一次。
```

### 5.4 定义 BUILTIN Handler 和登记入口

```python
# event_handlers/agent_handler.py
from openjiuwen.core.events import (
    Event, EventHandlerOps, EventNext, ExtensionEventBus, HandlerLevel,
)
from jiuwenswarm.extensions_v2.event_types.agent_event import (
    AGENT_EVENT_TYPE, AgentEventInput, AgentEventOutput,
)


async def on_agent_event(
    event: Event,
    ops: EventHandlerOps | None,
    next_handler: EventNext,
) -> AgentEventOutput:
    if not isinstance(event.event_input, AgentEventInput):
        raise TypeError("agent.custom requires AgentEventInput")
    # 前置检查或加工写在这里；短路也必须返回 AgentEventOutput。
    output = await next_handler(event)
    if not isinstance(output, AgentEventOutput):
        raise TypeError("agent.custom must return AgentEventOutput")
    # 后置处理写在这里；Ops 与 next 不保存到本次调用之外使用。
    return output


def register(bus: ExtensionEventBus) -> None:
    bus.register_event_handler(AGENT_EVENT_TYPE, on_agent_event, {}, HandlerLevel.BUILTIN)
    # 按需把 {} 改为匹配函数支持的字段模式，不能用此入口覆盖 CORE。
```

### 5.5 定义 Ops 能力和 Build Handler

需要其它 Handler 主动触发时增加 `ops.agent.dispatch`；只有业务入口触发时可以不增加这一能力。

```python
# handler_ops/agent_ops.py
from openjiuwen.core.events import (
    OPS_BUILD_EVENT_TYPE, Event, EventHandler, EventHandlerOps,
    EventHandlerOpsBuilder, EventNext, ExtensionEventBus, HandlerLevel,
)
from jiuwenswarm.extensions_v2.event_types.agent_event import (
    AGENT_EVENT_TYPE, AgentEventInput, AgentEventOutput,
)


def build_agent_ops_handler(bus: ExtensionEventBus) -> EventHandler:
    async def dispatch(ops_view: EventHandlerOps, request: AgentEventInput) -> AgentEventOutput:
        if ops_view.session_context is None:
            raise ValueError("ops.agent.dispatch requires SessionContext")
        # 调用能力时校验目标和允许范围，使用当前会话发起一条新事件链。
        output = await bus.trigger(Event(AGENT_EVENT_TYPE, request), ops_view.session_context)
        if not isinstance(output, AgentEventOutput):
            raise TypeError("agent.custom must return AgentEventOutput")
        return output

    async def agent_ops_build(
        event: Event, ops: EventHandlerOps | None, next_handler: EventNext,
    ) -> EventHandlerOpsBuilder:
        builder = await next_handler(event)
        if not isinstance(builder, EventHandlerOpsBuilder):
            raise TypeError("ops.build must return EventHandlerOpsBuilder")
        builder.add_namespace("agent")
        builder.add_method("agent", "dispatch", dispatch)
        return builder  # 只登记函数，不执行业务；返回取得的同一个 Builder。

    return agent_ops_build


def register(bus: ExtensionEventBus) -> None:
    bus.register_event_handler(
        OPS_BUILD_EVENT_TYPE, build_agent_ops_handler(bus), {}, HandlerLevel.BUILTIN,
    )
    # namespace 已有其它能力时合并进原主责 Build Handler，不再创建第二个。
```

### 5.6 接入启动登记

新增登记加到现有 `bootstrap.py`，保留其它领域条目。`assembly.py` 已将下面两个汇总函数作为回调传入唯一一次初始化，Registry 在回调返回后完成 Build，不另建初始化入口。

```python
# bootstrap.py：本章新增内容的接入示意。
from openjiuwen.core.events import ExtensionEventBus
from jiuwenswarm.extensions_v2.event_types.context_usage import register_context_usage_events
from jiuwenswarm.extensions_v2.event_types import agent_event
from jiuwenswarm.extensions_v2.event_handlers import agent_handler
from jiuwenswarm.extensions_v2.handler_ops import agent_ops
from jiuwenswarm.extensions_v2.webui.runtime import WebUIRuntime


def register_event_types(bus: ExtensionEventBus, webui: WebUIRuntime) -> None:
    webui.register_event_types(bus)      # 保留现有 UI 事件。
    register_context_usage_events(bus)  # 保留现有用量事件。
    agent_event.register(bus)


def register_handlers(bus: ExtensionEventBus, webui: WebUIRuntime) -> None:
    webui.register_handlers(bus)  # 保留现有 ui Ops Build Handler。
    agent_handler.register(bus)
    agent_ops.register(bus)  # 若不提供此能力，则不加入这条登记。
```

### 5.7 接通触发入口与结果返回

AgentServer 与 EventBus 在同一进程，直接在真实业务扩展点调用；不经过 WebUI 或 Gateway 适配器。

```python
# AgentServer 业务类的方法骨架，合并到实际业务调用位置。
from openjiuwen.core.events import Event, ExtensionEventBus, SessionContext
from jiuwenswarm.extensions_v2.event_types.agent_event import (
    AGENT_EVENT_TYPE, AgentEventInput, AgentEventOutput,
)


class AgentEventPublisher:
    def __init__(self, bus: ExtensionEventBus) -> None:
        self.bus = bus

    async def on_business_step(
        self, request: AgentEventInput, session: SessionContext,
    ) -> AgentEventOutput:
        # session 由服务端会话记录准备；不要读取全局“当前会话”。
        output = await self.bus.trigger(Event(AGENT_EVENT_TYPE, request), session)
        if not isinstance(output, AgentEventOutput):
            raise TypeError("agent.custom must return AgentEventOutput")
        # 检查 accepted、status，使用业务结果继续原流程，不再补跑默认动作。
        return output
```

### 5.8 完成后检查

单元测试覆盖类型唯一登记、匹配、继续与短路、输入错误、会话缺失、Ops Build 和结果使用。需要结果后再继续时直接 await；仅通知型事件可由业务安排后台任务，并负责异常、等待或取消。

## 6. 完成开发时核对哪些内容

### 6.1 检查清单

| 核对项 | 完成要求 |
| --- | --- |
| 事件契约 | 事件名至少两段、用点分隔，每段匹配 `[a-z][a-z0-9_]*`；输入输出和失败结果已明确，新模型采用 `frozen=True, slots=True` 的 dataclass |
| 文件与登记 | 三类文件职责明确，全部接入宿主回调，迁移前后没有重复登记 |
| 事件处理 | 一个事件只有一个 CORE；匹配函数同步返回 bool、不执行业务；继续、短路和输出符合约定 |
| Ops | Build Handler 在唯一一次 Build 前登记，构建时不执行业务；namespace 与方法名合法，同一 namespace 只创建一次 |
| 异常与验证 | 测试覆盖成功、非法输入、未命中、继续、短路及异常；入口按业务要求处理异常和取消，不假定事件失败会自动回滚 |
| 跨进程入口 | WebClient / Gateway 的方法、适配器和结果回传都已接通 |
| 本地业务 | 在真实扩展点触发并使用结果，后台任务由所属业务管理 |

### 6.2 常见错误

| 现象 | 优先检查 |
| --- | --- |
| `EVENTS_UNKNOWN_EVENT_INVALID` | 类型是否已登记，登记函数是否接入 `bootstrap.py` |
| `EVENTS_REGISTRATION_INVALID` | 是否重复登记同名类型，Handler 和匹配模式是否合法 |
| `EVENTS_STATE_INVALID` | 是否在 Build 前触发普通事件，或重复执行了 Build |
| `EVENTS_MATCH_INVALID` | 匹配函数是否抛错，或返回了非 bool 值 |
| `EVENTS_OPS_INVALID` | 是否重复创建 namespace、重复添加方法，或调用未知能力 |
| `EVENTS_NEXT_INVALID` | 是否跨调用保存 next、跨任务使用 next，或改变了 event_type |

真实接口可查阅 [ExtensionManager](../../management/registry.py)、[WebUIRuntime](../../webui/runtime.py)、[WebUIDispatchAdapter](../../webui/adapter.py)、core [Events 公共契约](../../../../../agent-core/openjiuwen/core/events/docs/specs/S_01_events-contract.md)和 [Registry 与 Bus 规约](../../../../../agent-core/openjiuwen/core/extension/docs/specs/S_02_registry-and-bus.md)。core 侧的对应写法见[内置事件开发说明](../../../../../agent-core/openjiuwen/core/events/docs/designs/events-extension-development-guide.md)。本文标为新增示例的文件与方法骨架仍需按业务实现。
