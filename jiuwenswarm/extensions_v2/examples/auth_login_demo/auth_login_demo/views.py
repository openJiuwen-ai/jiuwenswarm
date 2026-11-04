# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Declarative page composition and all user-facing example copy."""

from jiuwenswarm.gateway.extensions_v2.webui.models import RenderNode


def render_node(tree: dict) -> RenderNode:
    """Build the formal render model, including declared menu label nodes."""
    props = dict(tree.get("props", {}))
    field = "label" if tree["component"] == "Dropdown" else "children" if tree["component"] == "Tabs" else ""
    if field:
        props["items"] = [{**item, field: tuple(render_node(child) for child in item[field])}
                          if isinstance(item.get(field), list) else dict(item) for item in props["items"]]
    return RenderNode(component=tree["component"], props=props,
                      children=tuple(render_node(child) for child in tree.get("children", ())),
                      events=tree.get("events"))

COPY = {
    "zh": {
        "title": "欢迎使用WorkSwarm", "username": "用户名", "password": "密码",
        "username_required": "请输入用户名", "username_length": "用户名需为 4–64 个字符",
        "password_required": "请输入密码", "password_pattern": "密码需为 8–128 位，包含字母、数字和特殊字符",
        "submit": "登录", "promotion": "WorkSwarm 产品宣传图", "user": "当前用户",
        "logout": "退出登录", "invalid": "用户名或密码错误",
        "register": "注册并登录", "to_register": "没有账号？创建账号",
        "to_login": "已有账号？返回登录", "user_exists": "用户名已存在，请更换用户名或直接登录",
        "invalid_fields": "用户名需为 4–64 个字符，密码需为 8–128 个字符且包含字母、数字和特殊字符",
    },
    "en": {
        "title": "Welcome to WorkSwarm", "username": "Username", "password": "Password",
        "username_required": "Enter your username", "username_length": "Use 4–64 characters",
        "password_required": "Enter your password", "password_pattern": "Use 8–128 characters with a letter, a number and a special character",
        "submit": "Sign in", "promotion": "WorkSwarm promotional artwork", "user": "Current user",
        "logout": "Sign out", "invalid": "Incorrect username or password",
        "register": "Create account and sign in", "to_register": "New here? Create an account",
        "to_login": "Already registered? Sign in",
        "user_exists": "This username already exists. Choose another or sign in.",
        "invalid_fields": "Use 4–64 username characters and 8–128 password characters including a letter, number and special character",
    },
}


def text(value: str, *, size: str | None = None, strong: bool | None = None,
         text_type: str | None = None) -> dict:
    """Build one noninteractive text node."""
    props = {"value": value}
    for name, setting in (("size", size), ("strong", strong), ("type", text_type)):
        if setting is not None:
            props[name] = setting
    return {"component": "Text", "props": props}


def login_view(language: str, layout: str, assets: dict[str, str], *, mode: str = "login", message: str = "") -> dict:
    """Compose the full-viewport page with plugin-owned AntD properties."""
    copy = COPY[language]
    fields = [
        {"component": "Form.Item", "props": {"name": "username", "label": copy["username"], "rules": [
            {"required": True, "message": copy["username_required"]}, {"min": 4, "max": 64, "message": copy["username_length"]},
        ]}, "children": [{"component": "Input", "props": {"placeholder": copy["username"], "autoComplete": "off"}}]},
        {"component": "Form.Item", "props": {"name": "password", "label": copy["password"], "rules": [
            {"required": True, "message": copy["password_required"]},
            {"pattern": r"^(?=.{8,128}$)(?=.*[A-Za-z])(?=.*[0-9])(?=.*[^A-Za-z0-9]).+$", "message": copy["password_pattern"]},
        ]}, "children": [{"component": "Input", "props": {"type": "password", "placeholder": copy["password"], "autoComplete": "off"}}]},
    ]
    card = [text(copy["title"], size="lg", strong=True),
            {"component": "Form", "props": {"name": "credentials", "requiredMark": False}, "children": fields}]
    if message:
        card.append(text(copy[message], text_type="danger"))
    card.extend([
        {"component": "Button", "props": {"value": copy["register" if mode == "register" else "submit"], "type": "primary", "form": "credentials"},
         "events": {"ui.click": {"id": "login.register" if mode == "register" else "login.confirm"}}},
        {"component": "Button", "props": {"value": copy["to_login" if mode == "register" else "to_register"], "type": "text"},
         "events": {"ui.click": {"id": "login.mode", "args": {"mode": "login" if mode == "register" else "register"}}}},
    ])
    columns = []
    if layout == "split":
        columns.append({"component": "Box", "props": {"direction": "column", "flex": 1, "maxWidth": 560, "visibleFrom": "lg"},
                        "children": [{"component": "Image", "props": {"src": assets["promotion.png"], "alt": copy["promotion"],
                                      "width": "100%", "height": "auto", "fit": "contain", "preview": False}}]})
    columns.append({"component": "Box", "props": {"direction": "column", "width": 400, "maxWidth": "100%"},
                    "children": [{"component": "Card", "children": card}]})
    return {"component": "Box", "props": {"direction": "column", "width": "100%", "minHeight": "100%", "padding": "lg",
            "align": "center", "justify": "center", "backgroundImage": assets["background.svg"], "backgroundFit": "cover"},
            "children": [{"component": "Box", "props": {"direction": "row", "width": "100%", "maxWidth": 1200,
                          "marginInline": "auto", "align": "center", "justify": "center", "gap": "lg"}, "children": columns}]}


def user_view(language: str, avatar: str, username: str) -> dict:
    """Declare native AntD menu alignment and its action identifier."""
    copy = COPY[language]
    return {"component": "Dropdown", "props": {"items": [
            {"key": "account", "disabled": True, "label": [
                {"component": "Box", "props": {"direction": "row", "align": "center", "gap": "sm"},
                 "children": [{"component": "Avatar", "props": {"src": avatar, "alt": copy["user"], "size": 20}},
                              text(username)]},
            ]},
            {"type": "divider"}, {"key": "logout", "label": copy["logout"]}],
            "autoFocus": False, "destroyOnHidden": True, "styles": {"root": {"width": 200}},
            "placement": "topLeft", "align": {"points": ["bl", "tr"], "offset": [0, 0]}},
            "events": {"ui.select": {"id": "user.menu"}},
            "children": [{"component": "Avatar", "props": {"src": avatar, "alt": copy["user"]}}]}
