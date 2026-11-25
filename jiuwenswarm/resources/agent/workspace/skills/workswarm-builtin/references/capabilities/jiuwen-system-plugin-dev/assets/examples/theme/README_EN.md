# Theme Background

Animated backgrounds, interface colors and startup animations for WorkSwarm, with 4 built-in themes and support for importing your own theme packs.

## Features

- Application background: an image, video or animated page below the workspace, with adjustable dimming, blur and panel opacity; a still picture when reduced motion is on.
- Interface colors: a per-theme accent color and translucent panels.
- Startup animation: the current theme's animation plays when the app starts, with optional music and volume; play it on every start, on the first start of the day or only on manual preview, and hold Space to skip.
- Built-in themes: Research Terminal (科研终端), Blueprint (晨雾蓝图), Aurora (流光极昼) and Hive (蜂巢协同).
- Theme management: import theme packs for theme API v1 (`theme.json`) as a ZIP or folder, export a backup and restore from it.

## How it works

The plugin runs on the Gateway (`package.domain: gateway`) and uses the host's application-level UI capabilities:

1. Background: when the host mounts `application.background` it sends `ui.ready`; the plugin renders a media surface with `ops.ui.render`, using asset URLs from `ops.ui.asset_url`.
2. Colors: the plugin calls `ops.appearance.apply` with the accent color and translucency, and the host derives the interface colors from them; restoring the original look revokes them.
3. Startup animation: the host's startup gate sends `ui.ready` on `application.splash` to each splash provider in turn; the plugin plays or skips according to the play setting and receives `ui.closed` when the animation completes, is skipped or times out. "Preview startup" on the settings page calls `ops.splash.preview`.
4. Settings: the "Theme Background" entry in the settings navigation is declared on `application.settings`, and its interactions arrive as `ui.click` and `ui.change`. Preferences are kept in `ops.store`, imported themes in the plugin data directory from `ops.data.root()`, and backups are downloaded through `ops.ui.download`.
5. When a browser connection closes (`gateway.disconnect`), the plugin drops that connection's view state.

## Benefits

- No changes to the host page: backgrounds and startup animations run in sandboxed surfaces whose placement, timing, time limits and skipping are controlled by the host, so host upgrades are not broken by plugin styles.
- Text stays readable: the host derives the accent colors with contrast checks, translucency applies only while a background is shown, and popups and text stay legible.
- The home page never shows before the startup animation, and refreshing or reconnecting does not replay it.
- Suited to teams that want a consistent visual identity, or a custom startup animation for demos and showcases.
