"""The “主题背景” settings page (DESIGN.md section 4.3).

The tree uses only components the Gateway accepts for application slots. Every action carries its
target in the event ``args`` so the handler never trusts browser data for identity.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agent_gateway.extensions_v2.webui.models import RenderNode

from .catalog import Catalog, Theme

POLICY_OPTIONS = (
    ("theme", "跟随主题"), ("session", "每次启动"), ("daily", "每天首次"), ("version", "主题更新后"), ("manual", "仅手动预览"),
)
MOTION_OPTIONS = (("system", "跟随系统"), ("reduced", "减少动态"))
#: Theme cards per row: equal columns that fill the page width (four built-in themes make one row).
CARDS_PER_ROW = 4
SLIDER_WIDTH = 240
FEATURE_LABELS = {"image": "静态背景", "video": "视频背景", "html": "动态背景", "color": "纯色背景", "gradient": "渐变背景"}
Preview = Callable[[Theme], Awaitable[str | None]]


def node(component: str, props: dict[str, Any] | None = None, children: list[RenderNode] | tuple = (),
         events: dict[str, Any] | None = None) -> RenderNode:
    return RenderNode(component=component, props=props or {}, children=tuple(children), events=events)


def text(value: str, **props: Any) -> RenderNode:
    return node("Text", {"value": value, **props})


def row(*children: RenderNode, gap: str = "sm", justify: str | None = None, align: str = "center") -> RenderNode:
    props: dict[str, Any] = {"direction": "row", "gap": gap, "align": align, "wrap": True}
    if justify:
        props["justify"] = justify
    return node("Box", props, [child for child in children if child is not None])


def column(*children: RenderNode, gap: str = "md") -> RenderNode:
    return node("Box", {"direction": "column", "gap": gap}, [child for child in children if child is not None])


def button(value: str, action: str, *, args: dict[str, str] | None = None, **props: Any) -> RenderNode:
    event: dict[str, Any] = {"id": action}
    if args:
        event["args"] = args
    return node("Button", {"value": value, **props}, events={"ui.click": event})


def card_grid(cards: list[RenderNode]) -> RenderNode:
    """Rows of equal columns; a short last row is padded so its cards keep the column width."""
    rows = []
    for start in range(0, len(cards), CARDS_PER_ROW):
        chunk = cards[start:start + CARDS_PER_ROW]
        cells = [node("Box", {"direction": "column", "flex": 1}, [card]) for card in chunk]
        cells += [node("Box", {"direction": "column", "flex": 1}) for _ in range(CARDS_PER_ROW - len(chunk))]
        rows.append(node("Box", {"direction": "row", "gap": "md", "align": "start"}, cells))
    return column(*rows, gap="md")


def setting(label: str, control: RenderNode, hint: str | None = None) -> RenderNode:
    copy = column(text(label, strong=True), text(hint, type="secondary") if hint else None, gap="sm")
    return row(copy, control, gap="md", justify="between")


def slider(action: str, value: float, *, low: float, high: float, step: float, label: str) -> RenderNode:
    # A Slider has no width of its own; the box keeps it usable at the end of a setting row.
    return node("Box", {"direction": "column", "width": SLIDER_WIDTH}, [
        node("Slider", {"min": low, "max": high, "step": step, "value": value, "ariaLabel": label},
             events={"ui.change": {"id": action}})])


def _features(theme: Theme) -> list[str]:
    manifest = theme.pack.manifest
    labels = []
    background = manifest["background"]["type"]
    if background != "none":
        labels.append(FEATURE_LABELS.get(background, "背景"))
    if manifest["boot"]["type"] != "none":
        labels.append("开场动画")
    if manifest["boot"].get("audio"):
        labels.append("开场音乐")
    if manifest.get("appearance", {}).keys() - {"mascot"}:
        labels.append("界面配色")
    return labels


def theme_card(theme: Theme, *, active: bool, cover: str | None) -> RenderNode:
    tags = [node("Tag", {"children": "当前主题", "color": "success"})] if active else []
    if theme.source == "imported":
        tags.append(node("Tag", {"children": "已导入"}))
    tags += [node("Tag", {"children": label}) for label in _features(theme)]
    actions = [
        button("正在使用" if active else "使用", "use", args={"theme": theme.id}, type="primary", disabled=active),
        button("预览开场", "preview", args={"theme": theme.id}, disabled=theme.pack.manifest["boot"]["type"] == "none"),
    ]
    if theme.source == "imported":
        actions.append(button("删除", "delete", args={"theme": theme.id}, danger=True))
    props: dict[str, Any] = {"title": theme.pack.name, "size": "small"}
    if cover:
        props["cover"] = cover
    body = [text(theme.pack.manifest.get("description") or "没有说明", type="secondary"),
            row(*tags), row(*actions)]
    return node("Card", props, body)


def settings_page(catalog: Catalog, prefs: dict[str, Any], covers: dict[str, str], *,
                  notice: tuple[str, str] | None = None, busy: bool = False) -> RenderNode:
    settings = prefs["settings"]
    active = catalog.get(prefs.get("active_theme"))
    sections: list[RenderNode] = []
    if notice:
        tone, message = notice
        sections.append(text(message, type={"error": "danger", "success": "success"}.get(tone, "secondary")))
    cards = [theme_card(theme, active=active is not None and theme.id == active.id, cover=covers.get(theme.id))
             for theme in catalog.ordered()]
    sections.append(column(
        text("主题", strong=True, size="large"),
        card_grid(cards) if cards else node("Empty", {"description": "没有可用的主题"}),
        row(button("恢复原版界面", "original", disabled=active is None)),
    ))

    audio = active.pack.manifest["boot"].get("audio") if active else None
    volume = settings["boot_volume"]
    if volume is None:
        volume = audio.get("volume", 0.6) if audio else 0.6
    policy = settings["boot_policy"] or "theme"
    sections.append(column(
        node("Divider", {}),
        text("开场", strong=True, size="large"),
        setting("开场动画", node("Switch", {"checked": settings["boot_enabled"], "ariaLabel": "开场动画"},
                                  events={"ui.change": {"id": "boot_enabled"}}), "应用启动时播放当前主题的开场"),
        setting("播放时机", node("Select", {
            "options": [{"value": value, "label": label} for value, label in POLICY_OPTIONS], "value": policy,
            "ariaLabel": "播放时机"}, events={"ui.change": {"id": "boot_policy"}})),
        # A Form only holds Form.Item children; the submit button references it by name. The label is the
        # row's own text, like every other setting, so the field sits with the other controls on the right.
        setting("研究员代号", row(
            node("Form", {"name": "operator", "layout": "inline"}, [
                node("Form.Item", {"name": "operator_name"}, [
                    node("Input", {"defaultValue": settings["operator_name"], "maxLength": 24, "placeholder": "研究员",
                                   "ariaLabel": "研究员代号"}),
                ]),
            ]),
            button("保存代号", "operator", form="operator"),
        ), "部分主题的开场会显示这个代号"),
        setting("开场音乐", node("Switch", {"checked": settings["boot_audio"], "ariaLabel": "开场音乐"},
                                  events={"ui.change": {"id": "boot_audio"}})),
        setting("开场音量", slider("boot_volume", round(volume * 100), low=0, high=100, step=5, label="开场音量")),
    ))

    opacity = settings["surface_opacity"]
    if opacity is None:
        opacity = (active.pack.manifest.get("appearance", {}).get("surface_opacity") if active else None) or 0.86
    background = active.pack.manifest["background"] if active else {}
    dim = settings["background_dim"]
    dim = background.get("dim", 0) if dim is None else dim
    blur = settings["background_blur"]
    blur = background.get("blur", 0) if blur is None else blur
    sections.append(column(
        node("Divider", {}),
        text("背景与面板", strong=True, size="large"),
        setting("面板不透明度", slider("surface_opacity", round(opacity * 100), low=30, high=100, step=1,
                                      label="面板不透明度"), "有背景时侧栏与工作区的不透明度"),
        setting("背景暗化", slider("background_dim", round(dim * 100), low=0, high=90, step=1, label="背景暗化")),
        setting("背景模糊", slider("background_blur", round(blur), low=0, high=40, step=1, label="背景模糊")),
        setting("动态效果", node("Select", {
            "options": [{"value": value, "label": label} for value, label in MOTION_OPTIONS],
            "value": settings["motion"], "ariaLabel": "动态效果"}, events={"ui.change": {"id": "motion"}}),
            "减少动态时动态背景显示静态图"),
    ))

    failures = [text(f"{name}：{'；'.join(errors[:3])}", type="danger") for name, errors in catalog.failures]
    sections.append(column(
        node("Divider", {}),
        text("主题管理", strong=True, size="large"),
        row(
            node("FileImport", {"label": "导入主题包（ZIP）", "accept": [".zip"], "disabled": busy},
                 events={"ui.change": {"id": "import"}}),
            node("FileImport", {"label": "导入主题文件夹", "directory": True, "disabled": busy},
                 events={"ui.change": {"id": "import"}}),
        ),
        row(
            button("导出备份", "export", disabled=busy),
            node("FileImport", {"label": "从备份恢复", "accept": [".zip"], "disabled": busy},
                 events={"ui.change": {"id": "restore"}}),
            button("恢复默认设置", "reset", danger=True, disabled=busy),
        ),
        text("主题包需符合主题接口 v1（theme.json）。导入的主题保存在插件数据中，卸载插件时一并删除，请先导出备份。",
             type="secondary"),
        *failures,
    ))
    return column(*sections, gap="lg")
