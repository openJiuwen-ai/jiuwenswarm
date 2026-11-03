# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Render tree for the MCP Probe panel."""

from __future__ import annotations

from openjiuwen.core.events.ops_impl.mcp import McpCallOutput, McpConnectOutput, McpStatus

from jiuwenswarm.extensions_v2.webui.models import RenderNode

from .state import ProbeState

PACKAGE_ID = "example.mcp-probe"
SLOT_PANEL = "conversation.panel.right"
INSTANCE_KEY = "mcp-probe.panel"
ACTION_CONNECT = "mcp-probe.connect"
ACTION_CALL_ECHO = "mcp-probe.echo"
ACTION_CALL_INFO = "mcp-probe.info"
ACTION_REGISTER = "mcp-probe.register"
SERVER_NAME = "probe"
REGISTERED_SERVER = "probe-dynamic"
RESULT_LIMIT = 600


def panel_tree(state: ProbeState | None) -> RenderNode:
    """Build the panel root; an empty state still mounts a usable panel."""

    connected = state is not None and state.connect is not None and state.connect.connected
    children: list[RenderNode] = [_header(state, connected), _status(state)]
    register_error = _register_error(state)
    if register_error is not None:
        children.append(register_error)
    error = _error(state)
    if error is not None:
        children.append(error)
    children.extend((_tools(state), RenderNode("Divider"), _last_call(state)))
    body = RenderNode("Box", {"direction": "column", "gap": "sm"}, children=tuple(children))
    return RenderNode("Panel", {"title": "MCP Probe"}, children=(body,))


def _error(state: ProbeState | None) -> RenderNode | None:
    connect = state.connect if state is not None else None
    if connect is None or connect.status is McpStatus.OK or connect.error is None:
        return None
    return RenderNode(
        "Text",
        {
            "value": connect.error.message or connect.error.kind,
            "size": "sm",
            "tone": "danger",
            "monospace": True,
        },
    )


def _header(state: ProbeState | None, connected: bool) -> RenderNode:
    connect = state.connect if state is not None else None
    server_id = (connect.server_id if connect is not None else "") or "not connected"
    left = RenderNode(
        "Box",
        {"direction": "column", "gap": "none"},
        children=(
            RenderNode("Text", {"value": f"mcp 名称: {SERVER_NAME}", "size": "md", "weight": "medium"}),
            RenderNode(
                "Text",
                {
                    "value": f"ID: {server_id}",
                    "size": "sm",
                    "variant": "secondary",
                    "monospace": True,
                    "max_lines": 1,
                },
            ),
        ),
    )
    buttons = RenderNode(
        "Box",
        {"direction": "row", "gap": "sm", "wrap": True},
        children=(
            _button(
                "Register (runtime)",
                ACTION_REGISTER,
                "secondary" if _registered(state) else "primary",
                False,
            ),
            _button(
                "Connect (mcp.json)",
                ACTION_CONNECT,
                "primary" if not connected else "secondary",
                False,
            ),
            _button("Call echo", ACTION_CALL_ECHO, "secondary", not connected),
            _button("Call info", ACTION_CALL_INFO, "secondary", not connected),
        ),
    )
    return RenderNode(
        "Box",
        {"direction": "column", "gap": "sm"},
        children=(buttons, left),
    )


def _button(label: str, action_id: str, variant: str, disabled: bool) -> RenderNode:
    props: dict[str, object] = {"label": label, "variant": variant, "size": "sm"}
    if disabled:
        props["disabled"] = True
    return RenderNode(
        "Button", props, events={"ui.click": {"id": action_id, "args": {}}}
    )


def _registered(state: ProbeState | None) -> bool:
    return state is not None and state.register is not None


def _status(state: ProbeState | None) -> RenderNode:
    connect = state.connect if state is not None else None
    if connect is None:
        value = "connect idle"
        props: dict[str, object] = {"variant": "secondary"}
    else:
        parts = [f"connect {connect.status.value}"]
        if state.register is not None:
            parts.append("register ok")
        parts.append("runtime" if state.register is not None else "mcp.json")
        parts.append("agent visible" if connect.agent_visible else "agent not bound")
        value = " · ".join(parts)
        props = {"tone": _status_tone(connect)}
    props.update({"value": f"接口状态：{value}", "size": "sm", "max_lines": 1})
    return RenderNode("Text", props)


def _register_error(state: ProbeState | None) -> RenderNode | None:
    if state is None or not state.register_error:
        return None
    return RenderNode(
        "Text",
        {
            "value": f"register failed · {state.register_error}",
            "size": "sm",
            "tone": "danger",
            "monospace": True,
            "max_lines": 1,
        },
    )


def _tools(state: ProbeState | None) -> RenderNode:
    connect = state.connect if state is not None else None
    tools = connect.tools if connect is not None else ()
    if not tools:
        return RenderNode(
            "Text",
            {
                "value": "Connect to load the server tools.",
                "size": "sm",
                "variant": "secondary",
            },
        )
    rows: list[RenderNode] = [_tool_row("名称", "描述", header=True)]
    rows.extend(
        _tool_row(tool.name, tool.description or "no description") for tool in tools
    )
    table = RenderNode("Box", {"direction": "column", "gap": "sm"}, children=tuple(rows))
    return RenderNode(
        "Box",
        {"direction": "column", "gap": "sm"},
        children=(
            RenderNode(
                "Text", {"value": "MCP 工具信息", "size": "sm", "weight": "medium"}
            ),
            table,
        ),
    )


def _tool_row(name: str, description: str, *, header: bool = False) -> RenderNode:
    name_props: dict[str, object] = {"value": name, "size": "sm", "max_lines": 1}
    desc_props: dict[str, object] = {"value": description, "size": "sm", "max_lines": 2}
    if header:
        name_props["variant"] = "secondary"
        desc_props["variant"] = "secondary"
    else:
        name_props["weight"] = "medium"
        name_props["monospace"] = True
        desc_props["variant"] = "secondary"
    return RenderNode(
        "Box",
        {"direction": "row", "gap": "sm", "align": "start"},
        children=(
            RenderNode(
                "Box",
                {"direction": "row", "gap": "none", "flex": 2},
                children=(RenderNode("Text", name_props),),
            ),
            RenderNode(
                "Box",
                {"direction": "row", "gap": "none", "flex": 5},
                children=(RenderNode("Text", desc_props),),
            ),
        ),
    )


def _last_call(state: ProbeState | None) -> RenderNode:
    call = state.last_call if state is not None else None
    if call is None:
        return RenderNode(
            "Text",
            {"value": "No call yet.", "size": "sm", "variant": "secondary"},
        )
    tool = state.last_tool if state is not None and state.last_tool else "unknown"
    header = RenderNode(
        "Box",
        {"direction": "row", "gap": "sm", "wrap": True},
        children=(
            RenderNode(
                "Text",
                {"value": f"Last call : {tool}", "size": "sm", "weight": "medium"},
            ),
            _tag(call.status.value, _status_color(call.status)),
        ),
    )
    if call.error is not None:
        detail = call.error.message or call.error.kind
        tone = "danger"
    else:
        detail = _render_result(call)
        tone = "success"
    return RenderNode(
        "Box",
        {"direction": "column", "gap": "none"},
        children=(
            header,
            RenderNode(
                "Text",
                {
                    "value": detail,
                    "size": "sm",
                    "monospace": True,
                    "tone": tone,
                    "max_lines": 4,
                },
            ),
        ),
    )


def _render_result(call: McpCallOutput) -> str:
    value = str(call.result)
    if len(value) <= RESULT_LIMIT:
        return value
    return value[:RESULT_LIMIT] + "…"


def _tag(label: str, color: str) -> RenderNode:
    return RenderNode("Tag", {"children": label, "color": color, "variant": "outlined"})


def _status_color(status: McpStatus) -> str:
    if status is McpStatus.OK:
        return "success"
    if status in (McpStatus.TIMEOUT, McpStatus.DENIED):
        return "warning"
    if status is McpStatus.INVALID_INPUT or status is McpStatus.TOOL_NOT_FOUND:
        return "processing"
    return "error"


def _status_tone(connect: McpConnectOutput) -> str:
    if connect.status is McpStatus.OK:
        return "success" if connect.connected else "warning"
    if connect.status in (McpStatus.TIMEOUT, McpStatus.DENIED):
        return "warning"
    return "danger"
