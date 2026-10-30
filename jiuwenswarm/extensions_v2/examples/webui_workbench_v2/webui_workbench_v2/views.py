# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure render-tree builders using only the public component v2 contract."""

from __future__ import annotations

from jiuwenswarm.extensions_v2.webui.models import RenderNode

from .state import DEMO_ITEMS, SessionState

PACKAGE_ID = "webui.workbench-v2"
INSTANCE = "workbench"
TASK_INSTANCE = "task-inspector"
IFRAME_INSTANCE = "iframe-preview"
TURN_SLOTS = frozenset({"conversation.turn.content.after", "conversation.turn.footer.actions"})
# Static Iframe roots for the two main pages; unrelated actions must not republish them.
IFRAME_PAGE_SLOTS = frozenset({"application.menu.main", "task.function.main"})
SLOTS = (
    "application.entry",
    "application.menu", "application.menu.main", "task.function", "task.function.main",
    "application.settings", "conversation.input.above", "conversation.input.below",
    "conversation.panel.right", "conversation.turn.content.after", "conversation.turn.footer.actions",
    "conversation.header.title.after", "conversation.header.actions",
)


def text(value: str, *, secondary: bool = False, size: str = "md", weight: str = "regular") -> RenderNode:
    return RenderNode("Text", {
        "value": value, "size": size, "weight": weight, "variant": "secondary" if secondary else "default",
    })


def button(
    label: str, action: str, *, args: dict | None = None, disabled: bool = False,
    variant: str = "default", size: str = "sm",
) -> RenderNode:
    return RenderNode(
        "Button", {"label": label, "size": size, "variant": variant, "disabled": disabled},
        events={"ui.click": {"id": action, "args": args or {}}},
    )


def column(*children: RenderNode) -> RenderNode:
    return RenderNode("Box", {"direction": "column", "gap": "md"}, children=children)


def row(*children: RenderNode) -> RenderNode:
    return RenderNode("Box", {"direction": "row", "gap": "sm", "wrap": True, "align": "center"}, children=children)


def field(label: str, *children: RenderNode) -> RenderNode:
    return RenderNode("Box", {"direction": "column", "gap": "sm"}, children=(text(label), *children))


def switch(label: str, checked: bool, field: str) -> RenderNode:
    return RenderNode(
        "Switch", {"checked": checked, "ariaLabel": label},
        events={"ui.change": {"id": "workbench.option", "args": {"field": field}}},
    )


def filters(state: SessionState) -> RenderNode:
    """Use declared business args; control values arrive in normalized event data."""
    return column(
        field("筛选任务 / Filter tasks", RenderNode("Input", {
            "value": state.query, "placeholder": "搜索任务 / Search tasks", "allowClear": True,
            "maxLength": 80, "ariaLabel": "搜索任务 / Search tasks",
        }, events={
            "ui.change": {"id": "workbench.query", "args": {}},
            "ui.press_enter": {"id": "workbench.apply", "args": {}},
        })),
        field("任务分类 / Task category", RenderNode("Select", {
            "options": [
                {"value": "design", "label": "设计 / Design"},
                {"value": "test", "label": "测试 / Test"},
                {"value": "locked", "label": "不可选 / Unavailable", "disabled": True},
            ],
            "value": state.category, "allowClear": True, "showSearch": True,
            "placeholder": "全部分类 / All categories", "ariaLabel": "任务分类 / Task category",
        }, events={
            "ui.select": {"id": "workbench.category", "args": {}},
            "ui.search": {"id": "workbench.search", "args": {}},
            "ui.clear": {"id": "workbench.category.clear", "args": {}},
        })),
        field("项目 / Project", RenderNode("TreeSelect", {
            "treeData": [{
                "value": "root", "title": "演示项目 / Demo projects", "selectable": False,
                "children": [
                    {"value": "platform", "title": "平台 / Platform"},
                    {"value": "examples", "title": "示例 / Examples"},
                ],
            }],
            "value": state.project, "allowClear": True, "showSearch": True,
            "placeholder": "全部项目 / All projects",
            "treeDefaultExpandAll": True, "ariaLabel": "项目 / Project",
        }, events={
            "ui.change": {"id": "workbench.project", "args": {}},
            "ui.clear": {"id": "workbench.project.clear", "args": {}},
        })),
        field(f"最低分数 / Minimum score: {state.threshold}", RenderNode("Slider", {
            "min": 0, "max": 100, "step": 5, "value": state.threshold,
            "marks": {"0": "0", "50": "50", "100": "100"}, "ariaLabel": "最低分数 / Minimum score",
        }, events={"ui.change": {"id": "workbench.threshold", "args": {}}})),
        row(text("自动摘要 / Auto summary"), switch("自动摘要 / Auto summary", state.auto_summary, "auto_summary")),
        row(button("应用 / Apply", "workbench.apply", variant="primary", size="md"),
            button("重置 / Reset", "workbench.reset", size="md")),
        text(f"分类搜索 / Category search: {state.search_hint or '—'}", secondary=True),
    )


def results(state: SessionState) -> RenderNode:
    items = state.results()
    children = tuple(
        row(
            RenderNode("Tag", {"children": item.category, "color": "processing"}),
            text(f"{item.title} · {item.score}"),
            button("查看 / View", "workbench.details", args={"item": item.key}),
        ) for item in items
    )
    if not children:
        children = (RenderNode("Empty", {"image": "simple", "description": "没有匹配任务 / No matching tasks"}),)
    return RenderNode("Card", {"title": "筛选结果 / Results", "size": "small"}, children=children)


def details(state: SessionState) -> RenderNode:
    selected = next(item for item in DEMO_ITEMS if item.key == state.selected)
    return RenderNode("Card", {"title": "任务详情 / Task details", "size": "small"}, children=(
        text(selected.title),
        row(RenderNode("Tag", {"children": f"{selected.project} · {selected.score}", "color": "success"})),
        text(state.last_result),
        text(state.summary(), secondary=True),
    ))


def login_page() -> RenderNode:
    """Declare the presentation only; the buttons have no event bindings."""
    return RenderNode("Box", {
        "direction": "column", "align": "center", "justify": "center", "gap": "lg", "flex": 1,
    }, children=(
        RenderNode("Image", {
            "src": "/logo.svg", "alt": "账户 / Account", "width": 64, "height": 64, "preview": False,
        }),
        row(
            RenderNode("Button", {"label": "登录", "variant": "primary", "size": "md"}),
            RenderNode("Button", {"label": "退出", "variant": "primary", "size": "md"}),
        ),
    ))


def workspace_files(state: SessionState) -> RenderNode:
    """Combine independent file components inside the existing right contribution."""
    workspace = state.workspace
    tree_result = workspace.tree_result
    tree = RenderNode("FileTree", {
        "treeData": tree_result.get("treeData", []),
        "status": tree_result.get("status", "ready"),
        "truncated": tree_result.get("truncated", False),
        "readFailed": tree_result.get("readFailed", False),
    }, events={
        "ui.select": {"id": "workbench.files.select", "args": {}},
        "ui.change": {"id": "workbench.files.expand", "args": {}},
    })
    preview = RenderNode("FilePreview", {
        "file": workspace.preview_result.get("file"),
        "status": workspace.preview_result.get("status", "ready"),
    })
    return RenderNode("Panel", {"title": "工作区文件 / Workspace files"}, children=(
        RenderNode("Box", {"direction": "row", "gap": "none", "align": "stretch", "flex": 1},
                   children=(tree, preview)),
    ))


def iframe_page(title: str, src: str) -> RenderNode:
    """Iframe root for a menu or task main page; it owns the whole page content area."""
    return RenderNode("Iframe", {"title": title, "src": src})


def welcome_page() -> RenderNode:
    """Application-entry welcome card demonstrating Avatar(url=...) and entry close."""
    return RenderNode("Card", {"title": "欢迎使用插件交互工作台 / Welcome to Plugin Workbench"}, children=(
        RenderNode("Box", {
            "direction": "column", "align": "center", "justify": "center", "gap": "md",
        }, children=(
            RenderNode("Avatar", {
                "url": "/logo.svg", "alt": "欢迎 / Welcome", "size": 64, "shape": "circle",
            }),
            text(
                "欢迎体验 JiuwenSwarm WebUI 插件演示，支持应用级入口槽位与会话级交互槽位。 / "
                "Welcome to the JiuwenSwarm WebUI plugin demo.",
                secondary=True,
            ),
            button("开始使用 / Get started", "workbench.entry.close", variant="primary", size="md"),
        )),
    ))


def build_view(slot: str, state: SessionState, instance_key: str) -> RenderNode:
    """Build a complete replacement tree for one declared contribution."""
    if slot == "application.entry":
        return welcome_page()
    if slot == "application.menu":
        return button("交互工作台 / Workbench", "workbench.open")
    if slot == "task.function":
        return button("任务检查 / Inspect tasks", "workbench.open")
    if slot == "application.menu.main":
        return iframe_page("工作台页面 / Workbench page", "https://example.com/?page=application.menu.main")
    if slot == "task.function.main":
        return iframe_page("任务检查页面 / Task inspection page", "https://example.com/?page=task.function.main")
    if slot == "application.settings":
        return RenderNode("Box", {"direction": "row", "justify": "between", "gap": "md", "wrap": True}, children=(
            column(text("自动摘要 / Auto summary"), text("在本会话的辅助区域显示筛选摘要。 / Show filter summaries in this conversation.", secondary=True)),
            switch("自动摘要 / Auto summary", state.auto_summary, "auto_summary"),
        ))
    if slot == "conversation.input.above":
        return column(text(state.summary() if state.auto_summary else "自动摘要已暂停 / Auto summary paused", secondary=True),
                      text(state.last_result, secondary=True))
    if slot == "conversation.input.below":
        return text(f"工作台 / Workbench · {state.operations} 次操作 / operations", secondary=True)
    if slot == "conversation.panel.right":
        if instance_key == INSTANCE:
            return workspace_files(state)
        if instance_key == IFRAME_INSTANCE:
            return RenderNode("Iframe", {
                "title": "网页预览 / Web preview",
                "src": "https://example.com",
            })
        raise ValueError(f"undeclared right-panel instance: {instance_key}")
    if slot == "conversation.header.title.after":
        return row(
            RenderNode("Avatar", {"url": "/logo.svg", "alt": "工作台 / Workbench", "size": "small"}),
            RenderNode("Tag", {"children": "工作台 / Workbench", "color": "processing"}),
        )
    if slot == "conversation.header.actions":
        return button("刷新工作台 / Refresh workbench", "workbench.refresh")
    turn = state.turn(instance_key)
    if slot == "conversation.turn.content.after":
        return column(text(turn.summary, secondary=True), text(turn.result, secondary=True))
    if slot == "conversation.turn.footer.actions":
        return row(button("刷新本轮 / Refresh round", "workbench.turn.refresh"),
                   button("本轮详情 / Round details", "workbench.turn.details"))
    raise ValueError(f"undeclared workbench slot: {slot}")
