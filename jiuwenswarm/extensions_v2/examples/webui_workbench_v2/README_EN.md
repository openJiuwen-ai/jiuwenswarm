# Plugin Workbench

[中文](README.md)

`webui.workbench-v2` demonstrates 14 registered WebUI slots (15 contributions in total, with two instances in the right panel), covering catalog components such as `Avatar`, `Card`, `Dropdown`, `FileTree`, `FilePreview`, `Panel`, `Iframe`, `Switch`, `Button`, `Tag`, `Box`, and `Text`. The sidebar main page and task inspection main page use `Iframe` roots, while the right panel provides both a workspace files tab (`Panel` root combining `FileTree` and `FilePreview`) and a web preview tab (`Iframe` root). The plugin stores session state and platform file results, then publishes complete render trees. File components handle expansion, selection, and status messages locally. The demo calls no model or external business API.

## Install and activate

Run Gateway, AgentServer, and the Web frontend with this branch's WebUI protocol and runtime assembly. For hot installation, run `python scripts/install_workbench_plugin.py --zip-only` from the repository root to build `webui_workbench_v2.zip`. Without that flag, the script deploys directly to the install directory and requires a service restart; do not install the same version again from ZIP.

Place the generated ZIP where the AgentServer machine can read it, then run these commands in the Web input in single-agent mode:

```text
/extension install /path/to/webui_workbench_v2.zip
/extension query webui.workbench-v2
```

If an older package with the same ID is active, run `/extension uninstall webui.workbench-v2` first, then install the new ZIP and query it. Successful installation activates the package immediately. Startup loads installed copies and does not scan `examples/` in the source tree. Management commands work in a new empty conversation, session-scoped rendering requires a real session; application-scope slots (`application.entry`, `application.sidebar.footer`) do not.

After installation, open an existing real conversation and refresh or re-enter it so the Host sends `ui.ready`. The first ready builds the demo trees; later ready events restore the active extension's state. Ready for the file instance reads the root directory again and clears the preview; ready for the webpage instance republishes its Iframe.

## Open and interact

| Slot | Content and entry |
| --- | --- |
| `application.entry` | Welcome card and Avatar on first visit; Get started closes it. Accepted ready stores a visit marker, so refresh skips mounting |
| `application.sidebar.footer` | User menu at the bottom of the icon rail (`Avatar` + `Dropdown`, instance `user-menu`): application scope, single capacity |
| `application.menu` → `application.menu.main` | Click "交互工作台 / Workbench" in the sidebar to open the main page (`Iframe` root); both use instance `workbench` |
| `task.function` → `task.function.main` | Click "任务检查 / Inspect tasks" in the task area to open the task page (`Iframe` root); both use `task-inspector` |
| `application.settings` | "天气详情 / Weather details" switch in the settings content area, scoped to the current conversation |
| `conversation.input.above` | Token Weather demo with a hide/show button on the right, arranged horizontally in a `Box` with wrapping on narrow screens; the host provides a muted rounded strip above the activity panel in team mode |
| `conversation.input.below` | Four quick actions for weekly reports, bug fixes, presentations, and idle tasks, using `file-text`, `wrench`, `presentation`, and `clock` icons respectively |
| `conversation.panel.right` | "Workspace files" (`Panel` root, instance `workbench`) and "Web preview" (`Iframe` root, instance `iframe-preview`) tabs, mutually exclusive with native content |
| `conversation.session.content.after` | Real token usage for a completed assistant round, using the ghost Button style and database icon without a click action; round menu actions add a result row |
| `conversation.turn.footer.actions` | A `👍` symbol button beside an icon-only `…` dropdown containing "Refresh round" and "Round details" |
| `conversation.header.title.after` | Workbench status Tag |
| `conversation.header.actions` | Icon-only refresh button declared by the plugin with `variant="ghost"`, `icon="refresh"`, and `iconOnly=true` |

Normal buttons use `size="md"`: 32px height, 14px text, 16px horizontal padding, an 8px icon gap,
8px corners, and no default shadow. The host theme owns these metrics; the plugin only selects size.
The four quick actions below the input use this same normal size.

Turn slots use the actual `requestId` as `instance_key`. Usage appears as a label such as `用量：22.8M tokens`,
using `Button(variant="ghost", size="md", icon="database")` without a `ui.click` binding, for display only.
The total comes from `turn.total_tokens` returned by `ops.session.usage()`.
It represents the provider's round total, rather than context occupancy or a cumulative session bill.
The plugin listens for `context.usage.updated`, reads the bound host snapshot, and updates only the matching session and requestId.
Repeated notifications replace the value without adding it. Reported zero is displayed as `用量：0 tokens`;
missing totals show "not reported", without fixed values or a total inferred from input and output counts.

The first mount queries available usage; later ready events retain cached values. Notifications received before mounting are cached too.
"Refresh round" queries usage again, saves the current filter summary, and displays that round's refresh count.
"Round details" displays its requestId and saved summary. Other rounds and sessions do not overwrite these values.
Reactivating the plugin clears its cache. The public query provides only the latest available round, so recovery requires a matching requestId;
other historical rounds show "not reported". Host usage notifications or saved provider summaries are required;
runtime modes that provide neither keep the "not reported" display.

The round menu uses `Dropdown(icon="ellipsis", ariaLabel="本轮操作 / Round actions")` and keeps its muted background while open.
A plugin-rendered `Button(label="👍", variant="ghost")` appears on its left in a horizontal Box. This symbol button is for display and has no like action binding.
Opening the menu sends no business event. Selecting an item sends `ui.select` with action `workbench.turn.select` and data.key `refresh` or `details`.
Standalone Button nodes also support `icon="ellipsis", iconOnly=true, variant="ghost"`; label remains the accessible name and hover hint.

The above-input view displays fixed demo values: `Token 天气： ☀️ 晴 9% · 91k/1.0M · 5h 1% · $1.06`, followed by secondary text `上下文充裕`. These values do not represent live usage. This view belongs to Workbench and does not require the separate `token_weather` plugin. Hiding details keeps the `Token 天气` label and a show button. Visibility is scoped to the conversation, survives subsequent ready events, and resets when the plugin is activated again. The plugin uses `Box`, `Text`, and `Button` without a `Card`; the host slot supplies the background, border radius, and padding.

## Sidebar user entry

`application.sidebar.footer` is an application-scope single slot that needs no chat session. The plugin renders an `Avatar` trigger with a `Dropdown` menu whose only item, "退出登录 / Sign out", is bound to `ui.select`. Selecting it records the signed-out state per browser connection and renders a "登录 / Sign in" button instead; clicking that button (`ui.click`, action `workbench.user.login`) restores the menu. State is isolated by `target.connection_id`; the platform clears the instance on disconnect while the plugin keeps the demo state for the activation, and the next connection triggers `ui.ready` again. No real authentication is involved.

## Right panel: workspace files and web preview

The package declares two right-side contributions: `workbench.details` publishes a `Panel` root with instance `workbench`; `workbench.iframe` publishes an `Iframe` root with instance `iframe-preview`. A right-side ready event without an instance publishes both independent instances; ready with a valid instance restores only that member. The host creates tabs from their root titles.

The file tab uses `Panel(title, children)` and places `FileTree` and `FilePreview` side by side inside a `Box`. Both file components are registered independently for reuse by other plugins. See [F_07](../../docs/features/F_07_ui-contributions.md#44-文件能力目录与预览) for the component and platform API details.

Single-agent chat and code sessions can browse files. Project sessions use their bound project root; projectless sessions use their bound runtime workspace. Automatically allocated task workspaces include `work/` and `outputs/`; a Code profile default session workspace retains its actual directory layout. Team mode returns an unsupported status, and missing bindings return an unavailable status.

When the file instance receives `ui.ready`, it calls `ops.session.files` to read the root directory and clears the preview. Expanding a directory sends `expandedKeys` via `ui.change`; the plugin maps keys to paths in the current snapshot and calls `ops.session.files` again (up to 100 entries per directory, 512 entries total, 8 levels, and 96 KiB). Selecting a file calls `ops.session.read_file` and passes the result to `FilePreview`. UTF-8 text up to 16 KiB still uses content; larger text (up to 1 MiB), images, and PDFs use downloadUrl, handled inside FilePreview. Each URL serves a file snapshot valid for 10 minutes; select the file again to refresh an expired URL. PDFs use a native iframe after checking the same-origin download response type and PDF signature; display depends on the browser's built-in PDF viewer. Other binary files and oversized text display the corresponding status notices.

Drag the file tree's right divider to resize it, or focus the divider and use the left/right arrow keys. Directory and preview updates retain the scroll position and width. Clearing the instance or disconnecting resets them; they are not persisted to storage.

The webpage instance uses an `Iframe` root, which is only permitted at the root of slots that declare it (`conversation.panel.right`, `application.menu.main`, `task.function.main`). It cannot be nested inside `Panel`, `Box`, or other components, and accepts no children, events, or srcdoc. The host pins `sandbox="allow-scripts allow-forms allow-popups allow-modals allow-downloads"`.
The right-side Web preview loads `https://httpbin.org/forms/post` for manual checks of loading, text input, options and form submission. Its response declares neither `X-Frame-Options` nor a restrictive `frame-ancestors`, making it suitable for iframe integration checks; network availability still depends on the target site.
Ordinary clicks, input and navigation inside the iframe do not require same-origin access. The loading notice does not intercept pointer events. Relative pages also receive an opaque origin, so pages depending on storage or same-origin requests may be incompatible; do not add `allow-same-origin` as a workaround. See [F_08](../../docs/features/F_08_frontend-render.md#46-iframe-权限与点击交互) for the security decision.

## Reload and uninstall

After editing the installed copy identified in the installation response, run:

```text
/extension reload webui.workbench-v2
/extension query webui.workbench-v2
```

Reload reads the installed copy, not the original ZIP. After editing repository sources, sync changes to the installed copy or rebuild and reinstall the ZIP. To uninstall:

```text
/extension uninstall webui.workbench-v2
/extension query webui.workbench-v2
```

## Protocol and validation limits

Manifest `schemaVersion: 1`, `ui.apiVersion: 1`, and frame `schema_version: 1` describe separate formats. This package uses declared component events such as `events['ui.click']`. Business arguments come from the declared action's `args`; control values come from normalized event `data`. A dispatch response is a receipt, never a replacement tree. See [F_08](../../docs/features/F_08_frontend-render.md) for the shared version and validation strategy.

`application.entry` and `application.sidebar.footer` use application identity from the current Web connection, without `session_id`; all other slots require a real session. `application.menu`, `task.function` and `application.settings` do not mount in a new empty conversation. The right panel uses two stable instances with isolated events and updates; other single slots select the latest update. `message.assistant.meta` is outside this package's targets.

## Rebuild after editing sources

To rebuild the ZIP after source edits, run `python scripts/install_workbench_plugin.py --zip-only` from the repository root, or run the following Python snippet (the ZIP root contains `manifest.yaml` and `webui_workbench_v2/` directly):

```python
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

source = Path("jiuwenswarm/extensions_v2/examples/webui_workbench_v2").resolve()
archive = Path("webui_workbench_v2.zip").resolve()
with ZipFile(archive, "w", ZIP_DEFLATED) as output:
    for path in sorted(source.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            output.write(path, path.relative_to(source))
print(archive)
```
