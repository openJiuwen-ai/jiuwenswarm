# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Settings views and image validation owned by the resource-manager plugin."""

import hashlib
import io
import warnings
from pathlib import Path

from PIL import Image
from agent_gateway.extensions_v2.webui.models import RenderNode

MAX_IMAGE_BYTES = 10 * 1024 * 1024
STORE_KEY = "resource_preferences"


def import_image(root: Path, data: dict) -> str:
    """Validate the host-issued upload, then retain the original image bytes."""
    files = data["files"]
    if len(files) != 1 or not 0 < data["total_bytes"] <= MAX_IMAGE_BYTES:
        raise ValueError("Choose one image up to 10 MiB")
    directory = (root / data["path"]).resolve(strict=True)
    uploads = (root / ".uploads").resolve(strict=True)
    if not directory.is_relative_to(uploads):
        raise ValueError("Image is outside the upload directory")
    source = directory / files[0]["path"]
    if source.is_symlink() or source.parent != directory:
        raise ValueError("Choose one image file")
    content = source.read_bytes()
    if len(content) != data["total_bytes"]:
        raise ValueError("Image size does not match its receipt")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(content)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP", "GIF"} or image.width * image.height > 16_000_000:
                raise ValueError("Unsupported image format or dimensions")
            suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "GIF": ".gif"}[image.format]
            image.verify()
    name = hashlib.sha256(content).hexdigest() + suffix
    public = root / "public"
    public.mkdir(exist_ok=True)
    # Content-addressed immutable files preserve URLs already used by another connection.
    target = public / name
    if not target.exists():
        source.replace(target)
    return name


def settings_view(catalog: list[dict], values: dict, covers: dict[str, str], error: dict[str, str] | None, disabled: bool = False, *, sound: dict, sound_preview: str) -> RenderNode:
    """Render resource-specific controls with the existing localized WebUI components."""
    cards = []
    for resource in catalog:
        identity = resource["id"]
        fields = []
        for field in resource["settings"]:
            args = {"resource": identity, "key": field["key"]}
            value = values[identity][field["key"]]
            kind = field["kind"]
            props = {"ariaLabel": field["title"], "disabled": disabled}
            if kind == "select":
                control = RenderNode(component="Select", props={**props, "options": field["options"], "value": value},
                    events={"ui.change": {"id": "setting.set", "args": args}})
            elif kind == "switch":
                control = RenderNode(component="Switch", props={**props, "checked": value},
                    events={"ui.change": {"id": "setting.set", "args": args}})
            elif kind == "input":
                control = RenderNode(component="Input", props={**props, "value": value, "maxLength": field["maxLength"]},
                    events={"ui.press_enter": {"id": "setting.set", "args": args}})
            else:
                control = RenderNode(component="Box", props={"direction": "column", "gap": "sm"}, children=(
                    RenderNode(component="FileImport", props={"label": {"zh": "选择形象", "en": "Choose image"},
                        "accept": [".png", ".jpg", ".jpeg", ".webp", ".gif"], "disabled": disabled},
                        events={"ui.change": {"id": "setting.image", "args": args}}),
                    RenderNode(component="Button", props={"value": {"zh": "恢复内置形象", "en": "Restore built-in image"},
                        "disabled": disabled or not value}, events={"ui.click": {"id": "setting.reset", "args": args}}),
                ))
            row = [RenderNode(component="Text", props={"value": field["title"], "strong": True}), control]
            if "description" in field:
                row.append(RenderNode(component="Text", props={"value": field["description"], "type": "secondary", "size": "small"}))
            fields.append(RenderNode(component="Box", props={"direction": "column", "gap": "sm"}, children=tuple(row)))
        if not identity.startswith("builtin-"):
            fields.append(RenderNode(component="Button", props={"value": {"zh": "移除资源", "en": "Remove resource"},
                "danger": True, "disabled": disabled}, events={"ui.click": {"id": "resource.remove", "args": {"resource": identity}}}))
        body = (
            RenderNode(component="Image", props={"src": covers[identity], "alt": "", "width": "100%", "height": 120, "fit": "contain", "preview": False}),
            RenderNode(component="Text", props={"value": resource["name"], "strong": True}),
            RenderNode(component="Text", props={"value": resource["description"], "type": "secondary", "size": "small"}),
            *fields,
        )
        cards.append(RenderNode(component="Box", props={"width": 240, "maxWidth": "100%"}, children=(
            RenderNode(component="Card", children=(
                RenderNode(component="Box", props={"direction": "column", "gap": "md"}, children=body),
            )),
        )))
    children = [RenderNode(component="Text", props={"value": {
        "zh": "在这里导入完整形态包、调整各资源的设置。点击页面右下角的“切换应用形态”悬浮按钮，选择资源后进入；保存设置不会收起工作台。",
        "en": "Import complete form packages and adjust each resource here. Enter through the Switch application form floating button at the bottom right; saving settings keeps the workspace open.",
    }, "type": "secondary"})]
    if error:
        children.append(RenderNode(component="Text", props={"value": error, "type": "danger"}))
    children.extend((
        RenderNode(component="FileImport", props={"label": {"zh": "导入形态资源包（ZIP）", "en": "Import form resource (ZIP)"},
            "accept": [".zip"], "disabled": disabled or len(catalog) >= 16}, events={"ui.change": {"id": "resource.import"}}),
        RenderNode(component="Text", props={"value": {
            "zh": "默认蜜蜂使用固定形象和动态效果，无需设置。其他桌宠样式通过完整资源包添加，各资源可提供自己的设置，重启后保留。",
            "en": "The default bee has a fixed appearance and motion, with no settings. Add other companions through complete resource packages; each may define its own settings, which survive restarts.",
        }, "type": "secondary", "size": "small"}),
        RenderNode(component="Box", props={"direction": "row", "gap": "md", "wrap": True}, children=tuple(cards)),
    ))
    children.append(RenderNode(component="Card", children=(
        RenderNode(component="Box", props={"direction": "column", "gap": "md"}, children=(
            RenderNode(component="Text", props={"value": {"zh": "切换音效", "en": "Transition sound"}, "strong": True}),
            RenderNode(component="Box", props={"direction": "row", "gap": "md", "align": "center"}, children=(
                RenderNode(component="Text", props={"value": {"zh": "播放切换音效", "en": "Play transition sounds"}}),
                RenderNode(component="Switch", props={"checked": sound["enabled"], "disabled": disabled,
                    "ariaLabel": {"zh": "播放切换音效", "en": "Play transition sounds"}},
                    events={"ui.change": {"id": "sound.set", "args": {"key": "enabled"}}}),
            )),
            RenderNode(component="Select", props={"value": sound["preset"], "disabled": disabled,
                "ariaLabel": {"zh": "切换音效", "en": "Transition sound"}, "options": [
                    {"value": "whoosh", "label": {"zh": "咻", "en": "Whoosh"}},
                    {"value": "soft", "label": {"zh": "轻弹", "en": "Soft pop"}},
                ]}, events={"ui.change": {"id": "sound.set", "args": {"key": "preset"}}}),
            RenderNode(component="Text", props={"value": {"zh": f"音量 {sound['volume']}%", "en": f"Volume {sound['volume']}%"}}),
            RenderNode(component="Select", props={"value": sound["volume"], "disabled": disabled,
                "options": [{"value": volume, "label": {"zh": f"{volume}%", "en": f"{volume}%"}} for volume in range(0, 101, 5)],
                "ariaLabel": {"zh": "切换音效音量", "en": "Transition sound volume"}},
                events={"ui.change": {"id": "sound.set", "args": {"key": "volume"}}}),
            RenderNode(component="Button", props={"value": {"zh": "试听音效", "en": "Preview sounds"},
                "href": sound_preview, "target": "_blank", "disabled": disabled or not sound["enabled"] or sound["volume"] == 0}),
            RenderNode(component="Text", props={"value": {"zh": "支持音效的形态共用此设置。试听在独立页面打开，使用当前音量。",
                "en": "Forms supporting transition sounds share these preferences. Preview opens a separate page at the selected volume."},
                "type": "secondary", "size": "small"}),
        )),
    )))
    return RenderNode(component="Box", props={"direction": "column", "gap": "md"}, children=tuple(children))
