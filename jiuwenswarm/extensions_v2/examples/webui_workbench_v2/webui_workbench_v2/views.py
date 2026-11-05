# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Pure render-tree builders using only the public component v2 contract."""

from __future__ import annotations

from jiuwenswarm.extensions_v2.webui.models import RenderNode

from .state import SessionState

PACKAGE_ID = "webui.workbench-v2"
INSTANCE = "workbench"
TASK_INSTANCE = "task-inspector"
IFRAME_INSTANCE = "iframe-preview"
USER_INSTANCE = "user-menu"
USER_SELECT_ACTION = "workbench.user.select"
TURN_SELECT_ACTION = "workbench.turn.select"
USER_LOGIN_ACTION = "workbench.user.login"
WEATHER_HIDE_ACTION = "workbench.weather.hide"
WEATHER_SHOW_ACTION = "workbench.weather.show"
TURN_SLOTS = frozenset({"conversation.session.content.after", "conversation.turn.footer.actions"})
QUICK_LAUNCH_ACTION = "workbench.quick.launch"
QUICK_TASKS = (
    ("周报总结", "weekly_report", "file-text"),
    ("报错修复", "bug_fix", "wrench"),
    ("PPT 制作", "ppt", "presentation"),
    ("闲时任务", "idle_task", "clock"),
)
# Static Iframe roots for the two main pages; unrelated actions must not republish them.
IFRAME_PAGE_SLOTS = frozenset({"application.menu.main", "task.function.main"})
SLOTS = (
    "application.entry", "application.sidebar.footer",
    "application.menu", "application.menu.main", "task.function", "task.function.main",
    "application.settings", "conversation.input.above", "conversation.input.below",
    "conversation.panel.right", "conversation.session.content.after", "conversation.turn.footer.actions",
    "conversation.header.title.after", "conversation.header.actions",
)


def text(value: str, *, secondary: bool = False, size: str = "md", strong: bool = False) -> RenderNode:
    props: dict = {"value": value, "size": size}
    if secondary:
        props["type"] = "secondary"
    if strong:
        props["strong"] = True
    return RenderNode("Text", props)


def button(
    label: str | dict[str, str], action: str, *, args: dict | None = None, disabled: bool = False,
    type: str = "default", size: str = "md", icon: str | None = None,
) -> RenderNode:
    props: dict = {"value": label, "size": size, "type": type, "disabled": disabled}
    if icon:
        props["icon"] = icon
    return RenderNode(
        "Button", props,
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
    return RenderNode("Panel", {"title": "工作区文件"}, children=(
        RenderNode("Box", {"direction": "row", "gap": "none", "align": "stretch", "flex": 1},
                   children=(tree, preview)),
    ))


def iframe_page(title: str | dict[str, str], src: str) -> RenderNode:
    """Iframe root for a menu or task main page; it owns the whole page content area."""
    return RenderNode("Iframe", {"title": title, "src": src})


def task_function_view(slot: str) -> RenderNode:
    """Build task navigation without depending on any chat state."""
    if slot == "task.function":
        return button({"zh": "任务检查", "en": "Inspect tasks"}, "workbench.open")
    if slot == "task.function.main":
        return iframe_page({"zh": "任务检查页面", "en": "Task inspection page"},
                           "https://example.com/?page=task.function.main")
    raise ValueError(f"unknown task slot: {slot}")



def welcome_page() -> RenderNode:
    """Application-entry welcome card demonstrating Avatar(src=...) and entry close."""
    return RenderNode("Card", {"title": "欢迎使用插件交互工作台 / Welcome to Plugin Workbench"}, children=(
        RenderNode("Box", {
            "direction": "column", "align": "center", "justify": "center", "gap": "md",
        }, children=(
            RenderNode("Avatar", {
                "src": "/logo.svg", "alt": "欢迎 / Welcome", "size": 64, "shape": "circle",
            }),
            text(
                "欢迎体验 JiuwenSwarm WebUI 插件演示，支持应用级入口槽位与会话级交互槽位。 / "
                "Welcome to the JiuwenSwarm WebUI plugin demo.",
                secondary=True,
            ),
            button("开始使用 / Get started", "workbench.entry.close", type="primary", size="md"),
        )),
    ))


def user_menu_view() -> RenderNode:
    """Avatar trigger with profile and sign-out demos; no real authentication is involved."""
    return RenderNode(
        "Dropdown",
        {
            "items": [
                {"key": "profile", "label": "个人资料 / Profile"},
                {"key": "logout", "label": "退出登录 / Sign out", "danger": True},
            ],
            "placement": "topLeft",
            "ariaLabel": "用户菜单 / User menu",
        },
        events={"ui.select": {"id": USER_SELECT_ACTION, "args": {}}},
        children=(
            RenderNode("Avatar", {"src": "/logo.svg", "alt": "用户 / User", "size": 32, "shape": "circle"}),
        ),
    )


def signed_out_view() -> RenderNode:
    """Signed-out shell that switches back to the user menu."""
    return button("登录 / Sign in", USER_LOGIN_ACTION, type="primary")


def build_view(slot: str, state: SessionState, instance_key: str) -> RenderNode:
    """Build a complete replacement tree for one declared contribution."""
    if slot == "application.entry":
        return welcome_page()
    if slot == "application.sidebar.footer":
        # The connection-scoped path renders this slot; keep the builder total for direct callers.
        return user_menu_view()
    if slot == "application.menu":
        return button("交互工作台 / Workbench", "workbench.open")
    if slot in ("task.function", "task.function.main"):
        return task_function_view(slot)
    if slot == "application.menu.main":
        return iframe_page("工作台页面 / Workbench page", "https://example.com/?page=application.menu.main")
    if slot == "application.settings":
        return RenderNode("Box", {"direction": "row", "justify": "between", "gap": "md", "wrap": True}, children=(
            column(text("天气详情 / Weather details"), text("在输入框上方显示 Token 天气演示。 / Show the Token Weather demo above the input.", secondary=True)),
            switch("天气详情 / Weather details", state.weather_visible, "weather_visible"),
        ))
    if slot == "conversation.input.above":
        details = (
            text("Token 天气： ☀️ 晴 9% · 91k/1.0M · 5h 1% · $1.06"),
            text("上下文充裕", secondary=True),
        ) if state.weather_visible else (text("Token 天气", secondary=True),)
        return RenderNode("Box", {
            "direction": "row", "gap": "sm", "align": "center", "justify": "between",
        }, children=(
            RenderNode("Box", {
                "direction": "row", "gap": "sm", "align": "center", "wrap": True, "flex": 1,
            }, children=details),
            button("隐藏" if state.weather_visible else "显示",
                   WEATHER_HIDE_ACTION if state.weather_visible else WEATHER_SHOW_ACTION),
        ))
    if slot == "conversation.input.below":
        return RenderNode("Box", {
            "direction": "row", "gap": "sm", "wrap": True, "align": "center", "justify": "center",
        }, children=tuple(
            button(label, QUICK_LAUNCH_ACTION, args={"task": task}, icon=icon)
            for label, task, icon in QUICK_TASKS
        ))
    if slot == "conversation.panel.right":
        if instance_key == INSTANCE:
            return workspace_files(state)
        if instance_key == IFRAME_INSTANCE:
            return RenderNode("Iframe", {
                "title": {"zh": "网页预览", "en": "Web preview"},
                "src": "https://www.weather.com.cn/",
                "minWidth": 1280,
            })
        raise ValueError(f"undeclared right-panel instance: {instance_key}")
    if slot == "conversation.header.title.after":
        return RenderNode("Tag", {"children": "工作台 / Workbench", "color": "processing"})
    if slot == "conversation.header.actions":
        return RenderNode("Button", {
            "aria-label": "刷新工作台 / Refresh workbench", "type": "text", "size": "md",
            "icon": "refresh",
        }, events={"ui.click": {"id": "workbench.refresh", "args": {}}})
    turn = state.turn(instance_key)
    if slot == "conversation.session.content.after":
        usage = RenderNode("Button", {
            "value": turn.usage_label(), "type": "text", "size": "md", "icon": "database",
        })
        return column(usage, text(turn.result, secondary=True)) if turn.result_visible else usage
    if slot == "conversation.turn.footer.actions":
        return RenderNode("Box", {
            "direction": "row", "gap": "sm", "align": "center", "wrap": False,
        }, children=(
            RenderNode("Button", {"value": "👍", "type": "text", "size": "md"}),
            RenderNode("Dropdown", {
                "icon": "ellipsis",
                "ariaLabel": "本轮操作 / Round actions",
                "placement": "topRight",
                "items": [
                    {"key": "refresh", "label": "刷新本轮 / Refresh round"},
                    {"key": "details", "label": "本轮详情 / Round details"},
                ],
            }, events={"ui.select": {"id": TURN_SELECT_ACTION, "args": {}}}),
        ))
    raise ValueError(f"undeclared workbench slot: {slot}")
