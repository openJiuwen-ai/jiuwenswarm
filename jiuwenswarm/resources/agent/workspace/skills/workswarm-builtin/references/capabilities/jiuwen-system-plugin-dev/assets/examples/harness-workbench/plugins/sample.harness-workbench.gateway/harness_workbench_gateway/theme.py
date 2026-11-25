# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Application background and controlled appearance for each Harness preview."""

from __future__ import annotations

from dataclasses import dataclass

from agent_gateway.extensions_v2.webui.models import (
    RenderMode,
    RenderNode,
    RenderTarget,
    UiRenderInput,
)

BACKGROUND_SLOT = "application.background"
BACKGROUND_INSTANCE = "harness-workbench.background"


@dataclass(frozen=True, slots=True)
class HarnessTheme:
    """The package asset and host-controlled palette for one Harness."""

    asset: str
    title: str
    position: str
    appearance: dict[str, object]
    kind: str = "image"


THEMES = {
    "claude": HarnessTheme(
        asset="claude-brand-bg.png",
        title="Claude brand background",
        position="center",
        kind="image",
        appearance={
            "accent": "#D97757",
            "surface": "translucent",
            "surfaceOpacity": 0.82,
            "tokens": {
                "--color-scrollbar-thumb": "#B4532F",
                "--color-sidebar-nav-active": "rgba(217,119,87,0.36)",
                "--color-sidebar-nav-active-hover": "rgba(217,119,87,0.48)",
                "--color-conversation-active-surface": "rgba(217,119,87,0.3)",
                "--color-chat-accent-subtle": "rgba(217,119,87,0.24)",
                "--color-action-primary-subtle": "rgba(217,119,87,0.32)",
                "--color-action-primary-glow": "rgba(217,119,87,0.42)",
                "--color-border-accent": "#B4532F",
            },
        },
    ),
    "codex": HarnessTheme(
        asset="codex-brand-bg.png",
        title="Codex brand background",
        position="center",
        kind="image",
        appearance={
            "accent": "#6366F1",
            "surface": "translucent",
            "surfaceOpacity": 0.88,
            "tokens": {
                "--color-scrollbar-thumb": "rgba(99, 102, 241, 0.75)",
                "--color-sidebar-nav-active": "rgba(99, 102, 241, 0.36)",
                "--color-sidebar-nav-active-hover": "rgba(99, 102, 241, 0.48)",
                "--color-conversation-active-surface": "rgba(99, 102, 241, 0.3)",
                "--color-chat-accent-subtle": "rgba(99, 102, 241, 0.28)",
                "--color-action-primary-subtle": "rgba(99, 102, 241, 0.34)",
                "--color-action-primary-glow": "rgba(99, 102, 241, 0.46)",
                "--color-border-accent": "#6366F1",
            },
        },
    ),
}


async def apply_theme(ops, mode: str, connection_id: str) -> None:
    """Apply a Harness preview, or restore the host appearance for normal chat."""
    target = RenderTarget(
        "",
        BACKGROUND_SLOT,
        instance_key=BACKGROUND_INSTANCE,
        scope="application",
        connection_id=connection_id,
    )
    theme = THEMES.get(mode)
    if theme is None:
        await ops.ui.render(UiRenderInput(target=target, mode=RenderMode.CLEAR))
        await ops.appearance.apply(None)
        return
    source = await ops.ui.asset_url(theme.asset)
    root = RenderNode(
        "MediaSurface",
        {
            "kind": theme.kind,
            "src": source,
            "title": theme.title,
            "fit": "cover",
            "position": theme.position,
            "opacity": 1,
        },
    )
    await ops.ui.render(UiRenderInput(target=target, root=root))
    await ops.appearance.apply(theme.appearance)
