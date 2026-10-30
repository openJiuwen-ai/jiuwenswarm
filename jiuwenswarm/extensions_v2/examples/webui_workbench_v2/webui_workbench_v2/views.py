# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure render-tree builders using only the public component v2 contract."""

from __future__ import annotations

from jiuwenswarm.extensions_v2.webui.models import RenderNode

from .state import SessionState

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


def switch(label: str, checked: bool, field: str) -> RenderNode:
    return RenderNode(
        "Switch", {"checked": checked, "ariaLabel": label},
        events={"ui.change": {"id": "workbench.option", "args": {"field": field}}},
    )


def workspace_files(state: SessionState) -> RenderNode:
    """Combine independent file components inside the existing right contribution."""
    workspace = state.workspace
    tree_result = workspace.tree_result
    tree = RenderNode("FileTree", {
        "treeData": tree_result.get("treeData", []),
        "status": tree_result.get("status", "ready"),
        "truncated": tree_result.get("truncated", False),
        "readFailed": tree_result.get("readFailed", False),
        "message": tree_result.get("message", ""),
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
