# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure render-tree builders for the plugin-manager navigation entry and page.

The tree keeps at most two component levels below a Card: the slot content path
already consumes depth and the protocol budget is 8 levels in total.
"""

from __future__ import annotations

from jiuwenswarm.extensions_v2.webui.models import RenderNode

from .catalog import PluginEntry, SOURCE_LABELS
from .state import (
    FILTER_ALL,
    FILTER_DISABLED,
    FILTER_ENABLED,
    FILTER_UPDATABLE,
    NOTICE_INFO,
    NOTICE_SUCCESS,
    NOTICE_WARNING,
    TAB_ABOUT,
    TAB_PLUGINS,
    ManagerState,
)

PACKAGE_ID = "webui.plugin-manager"
# The navigation entry and its page share one instance identity: the page host
# subscribes with the instance key it received from the activated rail entry.
INSTANCE = "plugin-manager"

OPEN_ACTION = "plugin-manager.open"
REFRESH_ACTION = "plugin-manager.refresh"
INSTALL_ACTION = "plugin-manager.install"
SEARCH_ACTION = "plugin-manager.search"
FILTER_ACTION = "plugin-manager.filter"
TAB_ACTION = "plugin-manager.tab"
TOGGLE_ACTION = "plugin-manager.toggle"
DETAILS_ACTION = "plugin-manager.details"
CLOSE_DETAILS_ACTION = "plugin-manager.close-details"
MORE_ACTION = "plugin-manager.more"
BULK_ACTION = "plugin-manager.bulk"
DETAIL_TOGGLE_ACTION = "plugin-manager.detail.toggle"
DETAIL_RELOAD_ACTION = "plugin-manager.detail.reload"
DETAIL_UNINSTALL_ACTION = "plugin-manager.detail.uninstall"

FILTER_OPTIONS = (
    {"value": FILTER_ALL, "label": "全部状态 / All states"},
    {"value": FILTER_ENABLED, "label": "已启用 / Enabled"},
    {"value": FILTER_DISABLED, "label": "已停用 / Disabled"},
    {"value": FILTER_UPDATABLE, "label": "可更新 / Updatable"},
)

MORE_ITEMS = (
    {"key": "details", "label": "查看详情 / View details"},
    {"key": "reload", "label": "重新加载 / Reload"},
    {"key": "uninstall", "label": "卸载插件 / Uninstall", "danger": True},
    {"key": "folder", "label": "打开安装目录 / Open install folder"},
)

BULK_ITEMS = (
    {"key": "enable-all", "label": "全部启用 / Enable all"},
    {"key": "disable-all", "label": "全部停用 / Disable all"},
    {"key": "reset-demo", "label": "恢复演示数据 / Reset demo data"},
)


def text(
    value: str,
    *,
    secondary: bool = False,
    strong: bool = False,
    code: bool = False,
    size: str = "md",
    ellipsis: bool = False,
    tone: str = "",
) -> RenderNode:
    """Build a Text node with one semantic style at a time."""

    props: dict[str, object] = {"value": value, "size": size}
    if secondary:
        props["type"] = "secondary"
    if tone:
        props["type"] = tone
    if strong:
        props["strong"] = True
    if code:
        props["code"] = True
    if ellipsis:
        props["ellipsis"] = True
    return RenderNode("Text", props)


def button(
    label: str,
    action: str,
    *,
    args: dict[str, object] | None = None,
    kind: str = "default",
    size: str = "md",
    icon: str = "",
    danger: bool = False,
    disabled: bool = False,
) -> RenderNode:
    props: dict[str, object] = {"value": label, "type": kind, "size": size, "disabled": disabled}
    if icon:
        props["icon"] = icon
    if danger:
        props["danger"] = True
    return RenderNode("Button", props, events={"ui.click": {"id": action, "args": args or {}}})


def tag(label: str, color: str) -> RenderNode:
    return RenderNode("Tag", {"children": label, "color": color})


def box(
    direction: str,
    *children: RenderNode,
    gap: str = "md",
    align: str = "stretch",
    justify: str = "start",
    wrap: bool = False,
    flex: int | None = None,
) -> RenderNode:
    props: dict[str, object] = {
        "direction": direction,
        "gap": gap,
        "align": align,
        "justify": justify,
        "wrap": wrap,
    }
    if flex is not None:
        props["flex"] = flex
    return RenderNode("Box", props, children=children)


def icon(name: str, *, size: str = "medium") -> RenderNode:
    return RenderNode("Icon", {"name": name, "size": size})


def nav_view() -> RenderNode:
    """One navigation-rail button; the host supplies the rail artwork and styles."""

    return RenderNode(
        "Button",
        {"value": "插件管理", "type": "text", "size": "md"},
        events={"ui.click": {"id": OPEN_ACTION, "args": {}}},
    )


def _header() -> RenderNode:
    return box(
        "row",
        box(
            "column",
            text("插件管理 / Plugin manager", strong=True, size="large"),
            text(
                "管理本机插件：查看状态、启停、更新与卸载。本页由侧边栏顶层的 WebUI 插件渲染。 / "
                "Manage local plugins from a top-level WebUI plugin in the navigation rail.",
                secondary=True,
            ),
            gap="sm",
            flex=1,
        ),
        box(
            "row",
            button("刷新", REFRESH_ACTION, icon="refresh-cw"),
            button("安装插件", INSTALL_ACTION, kind="primary", icon="plus"),
            gap="sm",
            align="center",
        ),
        gap="md",
        align="start",
        justify="between",
        wrap=True,
    )


def _stat_card(label: str, value: int, hint: str) -> RenderNode:
    return RenderNode(
        "Card",
        {"size": "small", "variant": "outlined"},
        children=(
            box(
                "column",
                text(str(value), strong=True, size="large"),
                text(label, secondary=True),
                text(hint, secondary=True, size="small"),
                gap="none",
            ),
        ),
    )


def _stats(state: ManagerState) -> RenderNode:
    counts = state.counts()
    return box(
        "row",
        _stat_card("已安装 / Installed", counts["installed"], "本机插件目录 / Local plugin directory"),
        _stat_card("已启用 / Enabled", counts["enabled"], "下一次加载生效 / Applied on next load"),
        _stat_card("可更新 / Updatable", counts["updates"], "存在更高版本 / Newer version present"),
        _stat_card("已停用 / Disabled", counts["disabled"], "保留安装副本 / Installed copy kept"),
        gap="md",
        wrap=True,
    )


def _toolbar(state: ManagerState) -> RenderNode:
    return box(
        "column",
        RenderNode(
            "Input",
            {
                "value": state.query,
                "placeholder": "搜索插件名称、ID 或描述 / Search name, id or description",
                "ariaLabel": "搜索插件 / Search plugins",
                "allowClear": True,
                "size": "medium",
            },
            events={"ui.change": {"id": SEARCH_ACTION, "args": {}}},
        ),
        box(
            "row",
            RenderNode(
                "Select",
                {
                    "options": list(FILTER_OPTIONS),
                    "value": state.status_filter,
                    "ariaLabel": "状态筛选 / Status filter",
                    "size": "medium",
                },
                events={"ui.change": {"id": FILTER_ACTION, "args": {}}},
            ),
            RenderNode(
                "Dropdown",
                {
                    "items": list(BULK_ITEMS),
                    "icon": "ellipsis",
                    "ariaLabel": "批量操作 / Bulk actions",
                    "placement": "bottomRight",
                },
                events={"ui.select": {"id": BULK_ACTION, "args": {}}},
            ),
            gap="sm",
            align="center",
            wrap=True,
        ),
        gap="sm",
    )


def _status_tag(state: ManagerState, entry: PluginEntry) -> RenderNode:
    if entry.incompatible_reason:
        return tag("不兼容 / Incompatible", "warning")
    if state.is_enabled(entry):
        return tag("已启用 / Enabled", "success")
    return tag("已停用 / Disabled", "default")


def _entry_card(state: ManagerState, entry: PluginEntry) -> RenderNode:
    title_parts: list[RenderNode] = [
        icon(entry.icon, size="large"),
        text(entry.name, strong=True),
        tag(f"v{entry.version}", "processing"),
    ]
    if entry.update_available:
        title_parts.append(tag("可更新 / Update", "warning"))
    title_parts.extend((
        RenderNode(
            "Switch",
            {
                "checked": state.is_enabled(entry),
                "size": "small",
                "ariaLabel": f"启用 {entry.name}",
                "disabled": bool(entry.incompatible_reason),
            },
            events={"ui.change": {"id": TOGGLE_ACTION, "args": {"id": entry.plugin_id}}},
        ),
        button("详情", DETAILS_ACTION, args={"id": entry.plugin_id}, kind="text", size="small", icon="info"),
        RenderNode(
            "Dropdown",
            {
                "items": list(MORE_ITEMS),
                "icon": "ellipsis",
                "ariaLabel": f"更多操作 {entry.name}",
                "placement": "bottomRight",
            },
            events={"ui.select": {"id": MORE_ACTION, "args": {"id": entry.plugin_id}}},
        ),
    ))
    return RenderNode(
        "Card",
        {"size": "small", "variant": "outlined", "hoverable": True},
        children=(
            box("row", *title_parts, gap="sm", align="center", wrap=True, justify="between"),
            text(entry.description, secondary=True, ellipsis=True),
            box(
                "row",
                _status_tag(state, entry),
                tag(SOURCE_LABELS.get(entry.source, entry.source), "default"),
                text(entry.plugin_id, secondary=True, code=True, size="small"),
                text(f"· {entry.author}", secondary=True, size="small"),
                gap="sm",
                align="center",
                wrap=True,
            ),
        ),
    )


def _plugin_list(state: ManagerState) -> RenderNode:
    rows = state.visible()
    if not rows:
        return RenderNode(
            "Empty",
            {"description": "没有匹配的插件，试试调整搜索或筛选条件。/ No plugin matches the filters."},
        )
    return box("column", *(_entry_card(state, entry) for entry in rows), gap="sm")


def _about_tab() -> RenderNode:
    return box(
        "column",
        RenderNode(
            "Card",
            {"title": "关于本示例 / About this example", "size": "medium"},
            children=(
                box(
                    "column",
                    text("本页是 WebUI 插件系统的顶层插件示例，入口位于侧边栏导航区。 / "
                         "A top-level WebUI plugin example whose entry lives in the navigation rail."),
                    text("manifest.json 的 ui 段同时声明 application.menu 与 application.menu.main，"
                         "Handler 用 ops.ui.render 返回渲染树。 / The manifest declares both slots; "
                         "the handler renders trees with ops.ui.render."),
                    text("页面数据来自插件包内的演示目录，不代表真实扩展管理器状态。 / "
                         "Rows come from the bundled demo catalog, not the extension manager."),
                    gap="sm",
                ),
            ),
        ),
        RenderNode(
            "Card",
            {"title": "接入真实包管理 / Real package management", "size": "medium"},
            children=(
                box(
                    "column",
                    text("当前公开的 Handler Ops 只有 ui、session、http 与 gateway，没有扩展包管理能力。 / "
                         "Public handler ops expose ui, session, http and gateway only."),
                    text("安装真实插件请在输入框执行：", secondary=True),
                    text("/extension install <插件 ZIP 的绝对路径>", code=True),
                    text("查询、卸载与重载分别使用 /extension list、/extension uninstall <id>、"
                         "/extension reload <id>。", secondary=True),
                    gap="sm",
                ),
            ),
        ),
        box(
            "row",
            button("全部启用", BULK_ACTION, args={"key": "enable-all"}, icon="circle-check"),
            button("全部停用", BULK_ACTION, args={"key": "disable-all"}, icon="circle-x"),
            button("恢复演示数据", BULK_ACTION, args={"key": "reset-demo"}, icon="rotate-ccw"),
            gap="sm",
            wrap=True,
        ),
        gap="md",
    )


def _detail_card(state: ManagerState) -> RenderNode:
    entry = state.selected
    if entry is None:
        return RenderNode(
            "Card",
            {"title": "插件详情 / Plugin details", "size": "medium", "variant": "borderless"},
            children=(
                box(
                    "column",
                    RenderNode(
                        "Empty",
                        {"image": "simple", "description": "选择左侧插件查看详情 / Select a plugin on the left"},
                    ),
                    gap="sm",
                    align="center",
                    justify="center",
                ),
            ),
        )
    enabled = state.is_enabled(entry)
    meta_rows = (
        ("插件 ID / Plugin id", entry.plugin_id),
        ("版本 / Version", entry.version),
        ("最新版本 / Latest", entry.latest_version or "已是最新 / Up to date"),
        ("来源 / Source", SOURCE_LABELS.get(entry.source, entry.source)),
        ("权限 / Permission", entry.permission_label),
        ("安装目录 / Install path", entry.install_path),
    )
    meta = tuple(
        box(
            "row",
            text(label, secondary=True, size="small"),
            text(value, size="small"),
            gap="sm",
            justify="between",
            wrap=True,
        )
        for label, value in meta_rows
    )
    actions = (
        button(
            "停用" if enabled else "启用",
            DETAIL_TOGGLE_ACTION,
            args={"id": entry.plugin_id},
            kind="primary",
            disabled=bool(entry.incompatible_reason),
        ),
        button("重新加载", DETAIL_RELOAD_ACTION, args={"id": entry.plugin_id}, icon="rotate-ccw"),
        button(
            "卸载",
            DETAIL_UNINSTALL_ACTION,
            args={"id": entry.plugin_id},
            danger=True,
            icon="trash-2",
            disabled=not entry.can_uninstall,
        ),
        button("关闭", CLOSE_DETAILS_ACTION, kind="text", icon="x", args={}),
    )
    details: list[RenderNode] = [
        box(
            "row",
            icon(entry.icon, size="large"),
            box(
                "column",
                text(entry.name, strong=True),
                text(entry.description, secondary=True),
                gap="sm",
            ),
            gap="md",
            align="start",
        ),
    ]
    if entry.incompatible_reason:
        details.append(text(entry.incompatible_reason, tone="warning"))
    details.append(RenderNode("Divider", {}))
    details.append(box("column", *meta, gap="sm"))
    if entry.tags:
        details.append(box("row", *(tag(value, "default") for value in entry.tags), gap="sm", wrap=True))
    details.append(box("row", *actions, gap="sm", wrap=True))
    return RenderNode(
        "Card",
        {"title": "插件详情 / Plugin details", "extra": entry.name, "size": "medium"},
        children=tuple(details),
    )


def _tabs(state: ManagerState) -> RenderNode:
    items = [
        {
            "key": TAB_PLUGINS,
            "label": "插件列表 / Plugins",
            "children": [_toolbar(state), _plugin_list(state)],
        },
        {
            "key": TAB_ABOUT,
            "label": "概览 / Overview",
            "children": [_about_tab()],
        },
    ]
    return RenderNode(
        "Tabs",
        {"items": items, "activeKey": state.tab, "size": "medium"},
        events={"ui.change": {"id": TAB_ACTION, "args": {}}},
    )


def _footer(state: ManagerState) -> RenderNode:
    counts = state.counts()
    summary = (
        f"共 {counts['installed']} 个插件 · 启用 {counts['enabled']} · 停用 {counts['disabled']} · "
        f"可更新 {counts['updates']} · 操作 {state.operations} 次"
    )
    notice_props: dict[str, object] = {"value": state.notice or "就绪 / Ready"}
    if state.notice_type == NOTICE_SUCCESS:
        notice_props["type"] = "success"
    elif state.notice_type == NOTICE_WARNING:
        notice_props["type"] = "warning"
    elif state.notice_type == NOTICE_INFO:
        notice_props["type"] = "secondary"
    return box(
        "row",
        text(summary, secondary=True, size="small"),
        RenderNode("Text", notice_props),
        gap="sm",
        justify="between",
        align="center",
        wrap=True,
    )


def page_view(state: ManagerState) -> RenderNode:
    """Build one complete replacement tree for the menu main page."""

    return box(
        "column",
        _header(),
        _stats(state),
        box(
            "row",
            box("column", _tabs(state), gap="md", flex=2),
            box("column", _detail_card(state), gap="md", flex=1),
            gap="lg",
            align="start",
        ),
        _footer(state),
        gap="lg",
        flex=1,
    )
