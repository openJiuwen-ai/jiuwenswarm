# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Native slot trees. The selector is application-scoped; session chrome follows the binding."""

from __future__ import annotations

from jiuwenswarm.extensions_v2.webui.models import RenderNode

from .activity import ToolActivity, WorkbenchSnapshot

PACKAGE_ID = "sample.harness-workbench"
CLAUDE_MARK_ASSET = "claude-avatar.png"
CLAUDE_TITLE_MARK_ASSET = "claude-mark.svg"
CLAUDE_TITLE_FALLBACK_ASSET = "claude-mark.png"
CODEX_MARK_ASSET = "codex-avatar.png"
CODEX_MARK_FALLBACK = "codex-mark.png"
NORMAL_CHAT_ASSET = "normal-chat.png"
CLAUDE_PULSE_ASSET = "claude-pulse.webp"
CLAUDE_PULSE_FALLBACK = "claude-pulse-static.png"
CLAUDE_CTX_METER_ASSET = "claude-ctx-meter.webp"
CLAUDE_CTX_METER_FALLBACK = "claude-ctx-meter-static.png"
CODEX_LIVE_ASSET = "codex-live.webp"
CODEX_LIVE_FALLBACK = "codex-live-static.png"
COMPOSER_AVATAR_PX = 22
CHROME_AVATAR_PX = 18
CLAUDE_PULSE_PX = 16
CODEX_LIVE_PX = 12
CLAUDE_CTX_METER_W = 180
CLAUDE_CTX_METER_H = 6
FOOTER_TRIGGER_PX = 44
_CAPTION_TONE = {"normal": "secondary", "claude": "warning", "codex": "warning"}
_CAPTION_TAG_COLOR = {"normal": "default", "claude": "warning", "codex": "processing"}
_TRIGGER_ALT = {"normal": "普通对话", "claude": "Claude Code", "codex": "Codex"}
COMPOSER_SLOT = "conversation.input.above"
FOOTER_SLOT = "application.sidebar.footer"
CHOICE_SLOT = FOOTER_SLOT
STATUS_SLOT = COMPOSER_SLOT
_HARNESS_MENU = (
    {"key": "normal", "label": "普通对话"},
    {"key": "claude", "label": "Claude Code"},
    {"key": "codex", "label": "Codex"},
)
TITLE_SLOT = "conversation.header.title.after"
ACTIVITY_SLOT = "conversation.session.content.after"
PANEL_SLOT = "conversation.panel.right"
FOOTER_INSTANCE = "harness-workbench.choice"
COMPOSER_INSTANCE = "harness-workbench.composer"
STATUS_INSTANCE = COMPOSER_INSTANCE
TITLE_INSTANCE = "harness-workbench.title"
PANEL_INSTANCE = "harness-workbench.panel"
ACTION_MODE = "harness.next-task-mode"
CHOICES = ("normal", "claude", "codex")
_TAG_COLOR = {"Read": "processing", "Edit": "warning", "Bash": "default"}
_NATIVE_LABELS = ("Read", "Edit", "Bash")


def choice_tree(mode: str) -> RenderNode:
    """Compact Select used above the composer on real sessions."""
    selected = mode if mode in CHOICES else "normal"
    return RenderNode(
        "Select",
        {
            "value": selected,
            "size": "sm",
            "variant": "borderless",
            "ariaLabel": "切换 Harness",
            "options": (
                {"value": "normal", "label": "切换 harness：普通对话"},
                {"value": "claude", "label": "切换 harness：Claude"},
                {"value": "codex", "label": "切换 harness：Codex"},
            ),
        },
        events={"ui.select": {"id": ACTION_MODE, "args": {}}},
    )


def _footer_trigger_body(mode: str, *, trigger_src: str) -> RenderNode:
    """Large, mode-tinted trigger for the 72px icon rail."""
    selected = mode if mode in CHOICES else "normal"
    if trigger_src:
        return RenderNode(
            "Image",
            {
                "src": trigger_src,
                "alt": _TRIGGER_ALT[selected],
                "width": FOOTER_TRIGGER_PX,
                "height": FOOTER_TRIGGER_PX,
                "preview": False,
            },
        )
    return RenderNode(
        "Tag",
        {
            "children": "H",
            "color": _CAPTION_TAG_COLOR[selected],
            "variant": "filled",
        },
    )


def harness_menu_view(mode: str, *, trigger_src: str = "") -> RenderNode:
    """Menu on a large brand mark; picks the harness for the next new session."""
    trigger = RenderNode(
        "Box",
        {"direction": "column", "align": "center", "justify": "center"},
        children=(_footer_trigger_body(mode, trigger_src=trigger_src),),
    )
    return RenderNode(
        "Dropdown",
        {
            "ariaLabel": "切换 Harness",
            "trigger": "click",
            "placement": "topRight",
            "items": _HARNESS_MENU,
        },
        children=(trigger,),
        events={"ui.select": {"id": ACTION_MODE, "args": {}}},
    )


def harness_switch_view(mode: str, *, trigger_src: str = "") -> RenderNode:
    """Icon-rail footer: vivid trigger plus a mode-colored caption."""
    selected = mode if mode in CHOICES else "normal"
    tone = _CAPTION_TONE[selected]
    caption = RenderNode(
        "Box",
        {"direction": "column", "gap": "none", "align": "center"},
        children=(
            RenderNode("Text", {"value": "切换", "size": "md", "type": tone, "strong": True}),
            RenderNode("Text", {"value": "harness", "size": "sm", "type": tone, "strong": selected != "normal"}),
        ),
    )
    return RenderNode(
        "Box",
        {"direction": "column", "gap": "sm", "align": "center"},
        children=(harness_menu_view(selected, trigger_src=trigger_src), caption),
    )


def footer_choice_view(mode: str, *, trigger_src: str = "") -> RenderNode:
    """Footer slot uses the shared switch control."""
    return harness_switch_view(mode, trigger_src=trigger_src)


def _harness_avatar(
    src: str,
    alt: str,
    *,
    size: int,
    fallback: str = "",
) -> RenderNode:
    """Square avatar in the composer chrome (package asset URL from ``asset_url``)."""
    if not src and not fallback:
        return RenderNode("Tag", {"children": alt, "color": "default", "variant": "outlined"})
    return _chrome_image(src, alt, width=size, height=size, fallback=fallback)


def _codex_title_mark(codex_mark: str = "", *, codex_mark_fallback: str = "") -> RenderNode:
    """Single avatar after the conversation title — no duplicate labels."""
    if codex_mark or codex_mark_fallback:
        return _harness_avatar(
            codex_mark,
            "Codex",
            size=CHROME_AVATAR_PX,
            fallback=codex_mark_fallback,
        )
    return RenderNode("Icon", {"name": "file-code", "size": "small", "aria-label": "Codex"})


def _chrome_image(
    src: str,
    alt: str,
    *,
    width: int,
    height: int,
    fallback: str = "",
) -> RenderNode:
    """Animated or static raster from package assets (WebP loops in the host Image adapter)."""
    primary = src or fallback
    if not primary:
        return RenderNode("Tag", {"children": "●", "color": "default", "variant": "filled"})
    props: dict[str, object] = {
        "src": primary,
        "alt": alt,
        "width": width,
        "height": height,
        "preview": False,
    }
    if src and fallback and fallback != src:
        props["fallback"] = fallback
    return RenderNode("Image", props)


def _claude_live_dot(pulse_src: str = "", pulse_fallback: str = "") -> RenderNode:
    """Red pulse WebP (16px); falls back to static PNG then a tag."""
    return _chrome_image(
        pulse_src,
        "Claude live",
        width=CLAUDE_PULSE_PX,
        height=CLAUDE_PULSE_PX,
        fallback=pulse_fallback,
    )


def _codex_live_dot(live_src: str = "", live_fallback: str = "") -> RenderNode:
    """Green pulse WebP (12px) ahead of the Codex mark."""
    return _chrome_image(
        live_src,
        "Codex live",
        width=CODEX_LIVE_PX,
        height=CODEX_LIVE_PX,
        fallback=live_fallback,
    )


def _claude_input_bar(
    claude_mark: str,
    *,
    pulse_src: str = "",
    pulse_fallback: str = "",
    meter_src: str = "",
    meter_fallback: str = "",
) -> RenderNode:
    """Claude composer strip with animated pulse and optional context meter."""
    lead = RenderNode(
        "Box",
        {"direction": "row", "gap": "sm", "align": "center", "wrap": True},
        children=(
            _claude_live_dot(pulse_src, pulse_fallback),
            _claude_badge(claude_mark, size=COMPOSER_AVATAR_PX),
            RenderNode("Text", {"value": "Claude Code", "size": "sm", "strong": True}),
        ),
    )
    if not meter_src:
        return lead
    meter = _chrome_image(
        meter_src,
        "Context usage",
        width=CLAUDE_CTX_METER_W,
        height=CLAUDE_CTX_METER_H,
        fallback=meter_fallback,
    )
    return RenderNode(
        "Box",
        {"direction": "row", "gap": "sm", "align": "center", "justify": "between"},
        children=(lead, meter),
    )


def _codex_input_bar(
    codex_mark: str,
    *,
    live_src: str = "",
    live_fallback: str = "",
    codex_mark_fallback: str = "",
) -> RenderNode:
    """Codex composer strip; scan line animates in Gateway codex-paper.html."""
    return RenderNode(
        "Box",
        {"direction": "row", "gap": "sm", "align": "center", "wrap": True},
        children=(
            _codex_live_dot(live_src, live_fallback),
            _harness_avatar(
                codex_mark,
                "Codex",
                size=COMPOSER_AVATAR_PX,
                fallback=codex_mark_fallback,
            ),
            RenderNode(
                "Tag",
                {"children": "Full access", "color": "warning", "variant": "outlined"},
            ),
        ),
    )


def _claude_badge(mark_src: str = "", *, size: int = CHROME_AVATAR_PX) -> RenderNode:
    """Claude avatar when the package asset is available, otherwise the text tag."""
    if mark_src:
        return _harness_avatar(mark_src, "Claude Code", size=size)
    return RenderNode("Tag", {"children": "Claude Code", "color": "warning", "variant": "outlined"})


def _harness_switch_row(selected: str) -> RenderNode:
    """Always expose the workflow menu and the compact Select."""
    picked = selected if selected in CHOICES else "normal"
    return RenderNode(
        "Box",
        {"direction": "row", "gap": "sm", "align": "center", "wrap": True},
        children=(harness_menu_view(picked), choice_tree(picked)),
    )


def composer_view(
    selected: str,
    *,
    bound: bool = False,
    bound_mode: str = "normal",
    claude_mark: str = "",
    codex_mark: str = "",
    claude_pulse: str = "",
    claude_pulse_fallback: str = "",
    claude_meter: str = "",
    claude_meter_fallback: str = "",
    codex_live: str = "",
    codex_live_fallback: str = "",
    codex_mark_fallback: str = "",
) -> RenderNode:
    """Harness chrome above the composer. Switching lives in the sidebar footer only."""
    if not bound:
        return RenderNode(
            "Text",
            {
                "value": "在左侧图标栏底部切换 Harness",
                "type": "secondary",
                "size": "sm",
            },
        )
    claude_bar = lambda: _claude_input_bar(
        claude_mark,
        pulse_src=claude_pulse,
        pulse_fallback=claude_pulse_fallback,
        meter_src=claude_meter,
        meter_fallback=claude_meter_fallback,
    )
    codex_bar = lambda: _codex_input_bar(
        codex_mark,
        live_src=codex_live,
        live_fallback=codex_live_fallback,
        codex_mark_fallback=codex_mark_fallback,
    )
    if bound_mode == "claude":
        return claude_bar()
    if bound_mode == "codex":
        return codex_bar()
    return status_view(
        bound_mode,
        pending=False,
        claude_mark=claude_mark,
        codex_mark=codex_mark,
        claude_pulse=claude_pulse,
        claude_pulse_fallback=claude_pulse_fallback,
        claude_meter=claude_meter,
        claude_meter_fallback=claude_meter_fallback,
        codex_live=codex_live,
        codex_live_fallback=codex_live_fallback,
        codex_mark_fallback=codex_mark_fallback,
    )


def panel_view(sources: dict[str, str]) -> RenderNode:
    """Sandboxed right-hand Claude Code workbench."""
    return RenderNode(
        "Iframe",
        {"title": "Claude Code", "src": sources, "minWidth": 400},
    )


def status_view(
    mode: str,
    *,
    pending: bool = False,
    claude_mark: str = "",
    codex_mark: str = "",
    claude_pulse: str = "",
    claude_pulse_fallback: str = "",
    claude_meter: str = "",
    claude_meter_fallback: str = "",
    codex_live: str = "",
    codex_live_fallback: str = "",
    codex_mark_fallback: str = "",
) -> RenderNode:
    """Composer row. Pending names the next task; bound names this session."""
    if mode == "codex":
        return _codex_input_bar(
            codex_mark,
            live_src=codex_live,
            live_fallback=codex_live_fallback,
            codex_mark_fallback=codex_mark_fallback,
        )
    elif mode == "claude":
        if pending:
            return _claude_input_bar(
                claude_mark,
                pulse_src=claude_pulse,
                pulse_fallback=claude_pulse_fallback,
                meter_src=claude_meter,
                meter_fallback=claude_meter_fallback,
            )
        badge = _claude_badge(claude_mark, size=COMPOSER_AVATAR_PX)
        description = "Claude Code · 当前会话已绑定"
    else:
        badge = RenderNode("Tag", {"children": "普通对话", "color": "default"})
        description = "JiuwenSwarm 原生运行时"
    children = (
        badge,
        RenderNode("Text", {"value": description, "type": "secondary", "size": "sm"}),
    )
    return RenderNode(
        "Box",
        {"direction": "row", "gap": "sm", "align": "center", "justify": "between"},
        children=children,
    )


def title_view(
    mode: str,
    *,
    claude_mark: str = "",
    claude_title_fallback: str = "",
    codex_mark: str = "",
    codex_mark_fallback: str = "",
) -> RenderNode:
    """Native mark placed after the conversation title."""
    if mode == "codex":
        return _codex_title_mark(codex_mark, codex_mark_fallback=codex_mark_fallback)
    if mode != "claude":
        return RenderNode(
            "Box",
            {"direction": "row", "gap": "sm", "align": "center"},
            children=(),
        )
    return RenderNode(
        "Box",
        {"direction": "row", "gap": "sm", "align": "center"},
        children=(
            _chrome_image(
                claude_mark,
                "Claude",
                width=CHROME_AVATAR_PX,
                height=CHROME_AVATAR_PX,
                fallback=claude_title_fallback,
            ),
            RenderNode("Text", {"value": "Claude Code", "size": "sm", "strong": True}),
        ),
    )


def activity_view(snapshot: WorkbenchSnapshot) -> RenderNode:
    """Inline activity after the latest assistant reply."""
    if snapshot.mode == "claude":
        return claude_inline_activity_view(snapshot)
    if snapshot.mode == "codex":
        return codex_inline_activity_view(snapshot)
    rows = _tool_rows(snapshot)
    body = rows or (RenderNode("Text", {"value": "本轮还没有 Read / Edit / Bash", "type": "secondary", "size": "sm"}),)
    return RenderNode(
        "Card",
        {"title": "工具活动", "size": "small", "variant": "outlined"},
        children=body,
    )


def codex_inline_activity_view(snapshot: WorkbenchSnapshot) -> RenderNode:
    """Blueprint-style diff summary under the latest reply."""
    body = _codex_summary(snapshot)
    if len(body) == 1:
        body = (
            RenderNode("Text", {
                "value": "等待 Edit / Bash 产出变更摘要…",
                "type": "secondary",
                "size": "sm",
                "code": True,
            }),
        )
    header = RenderNode("Text", {
        "value": "── Codex · change stream ──",
        "type": "secondary",
        "size": "sm",
        "code": True,
    })
    return RenderNode(
        "Card",
        {"title": "变更流", "size": "small", "variant": "borderless"},
        children=(header, *body),
    )


def claude_inline_activity_view(snapshot: WorkbenchSnapshot) -> RenderNode:
    """Terminal-style tool stream with a labeled block."""
    rows = _claude_tool_stream(snapshot)
    if not rows:
        rows = (RenderNode("Text", {"value": "本轮还没有 Read / Edit / Bash", "type": "secondary", "size": "sm", "code": True}),)
    header = RenderNode("Text", {
        "value": "── Claude Code · tool stream ──",
        "type": "secondary",
        "size": "sm",
        "code": True,
    })
    return RenderNode(
        "Card",
        {"title": "工具流", "size": "small", "variant": "borderless"},
        children=(header, *rows),
    )


def _tool_text(item: ToolActivity) -> dict[str, object]:
    props: dict[str, object] = {"value": item.target or item.tool, "size": "sm", "ellipsis": True}
    if item.error:
        props["type"] = "danger"
    return props


def _count(snapshot: WorkbenchSnapshot, label: str) -> int:
    return sum(1 for item in snapshot.activities if item.label == label)


def _codex_summary(snapshot: WorkbenchSnapshot) -> tuple[RenderNode, ...]:
    """Black and white summary. Added lines are green and removed lines are red."""
    rows: list[RenderNode] = [
        RenderNode("Text", {
            "value": f"变更 {_count(snapshot, 'Edit')} · 命令 {_count(snapshot, 'Bash')}",
            "size": "sm",
        }),
    ]
    shown = 0
    for item in snapshot.activities:
        if item.label != "Edit":
            continue
        if item.target and item.diff:
            rows.append(RenderNode("Text", {"value": item.target, "code": True, "size": "sm", "ellipsis": True}))
        elif item.target:
            rows.append(RenderNode("Text", {"value": f"已修改 {item.target}", "size": "sm", "ellipsis": True}))
            rows.append(RenderNode(
                "Text",
                {"value": "Diff 数据未由 Harness 提供", "type": "secondary", "size": "sm"},
            ))
        for line in item.diff:
            if shown == 8:
                break
            rows.append(RenderNode("Text", {
                "value": f"{'+' if line.kind == 'add' else '-'} {line.text}",
                "type": "success" if line.kind == "add" else "danger",
                "code": True,
                "size": "sm",
            }))
            shown += 1
        if shown == 8:
            break
    has_edit = any(item.label == "Edit" and item.target for item in snapshot.activities)
    if shown == 0 and not has_edit:
        rows.append(RenderNode("Text", {"value": "还没有代码变更", "type": "secondary", "size": "sm"}))
    return tuple(rows)


def _claude_tool_stream(snapshot: WorkbenchSnapshot) -> tuple[RenderNode, ...]:
    activities = [item for item in snapshot.activities if item.label in _NATIVE_LABELS][:8]
    rows: list[RenderNode] = []
    diff_budget = 8
    for item in activities:
        rows.append(RenderNode("Text", {
            "value": item.label,
            "size": "sm",
            "code": True,
            "strong": True,
            "type": "secondary",
        }))
        target = item.target or item.tool
        text_props: dict[str, object] = {"value": target, "size": "sm", "code": True, "ellipsis": True}
        if item.error:
            text_props["type"] = "danger"
        rows.append(RenderNode("Text", text_props))
        if item.label == "Edit" and item.target and not item.diff:
            rows.append(RenderNode(
                "Text",
                {"value": "Diff 数据未由 Harness 提供", "type": "secondary", "size": "sm", "code": True},
            ))
        for line in item.diff:
            if diff_budget == 0:
                break
            rows.append(RenderNode("Text", {
                "value": f"{'+' if line.kind == 'add' else '-'} {line.text}",
                "type": "success" if line.kind == "add" else "danger",
                "code": True,
                "size": "sm",
            }))
            diff_budget -= 1
    return tuple(rows)


def _tool_rows(snapshot: WorkbenchSnapshot) -> tuple[RenderNode, ...]:
    activities = [item for item in snapshot.activities if item.label in _NATIVE_LABELS][:6]
    rows: list[RenderNode] = []
    shown_diff_lines = 0
    for item in activities:
        rows.append(RenderNode(
            "Box",
            {"direction": "row", "gap": "sm", "align": "center"},
            children=(
                RenderNode("Tag", {"children": item.label, "color": _TAG_COLOR[item.label]}),
                RenderNode("Text", _tool_text(item)),
            ),
        ))
        if item.label != "Edit":
            continue
        if item.target and not item.diff:
            rows.append(RenderNode(
                "Text",
                {"value": "Diff 数据未由 Harness 提供", "type": "secondary", "size": "sm"},
            ))
        for line in item.diff:
            if shown_diff_lines == 8:
                break
            rows.append(RenderNode("Text", {
                "value": f"{'+' if line.kind == 'add' else '-'} {line.text}",
                "type": "success" if line.kind == "add" else "danger",
                "code": True,
                "size": "sm",
            }))
            shown_diff_lines += 1
    return tuple(rows)
