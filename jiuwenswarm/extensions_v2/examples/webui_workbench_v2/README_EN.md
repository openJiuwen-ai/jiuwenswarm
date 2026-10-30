# Plugin Workbench

[中文](README.md)

`webui.workbench-v2` demonstrates thirteen WebUI slots and right-side `Panel` and `Iframe` roots. The sidebar page is a presentation-only login shell, while task inspection uses fixed data, the file tab reads the current session's actual workspace, and the webpage tab loads the example.com page directly. The plugin saves filters and platform file results, then publishes complete render trees. File components manage expansion, selection and status messages. The demo calls no model or external business API.

## Install and activate

Run Gateway, AgentServer and the Web frontend with this change's protocol and production assembly. Copying the example into an older service does not add the component support. From the repository root, `uv run jiuwenswarm-start debug --skip-build` starts services using an existing frontend build. To rebuild without static checks, run `npm exec vite build` in the frontend directory, following repository rules.

A ready-to-install [webui_workbench_v2.zip](../../../../webui_workbench_v2.zip) is available in the repository root. You can install it directly for manual testing. Place it where the AgentServer machine can read it, then enter these commands in the Web input in single Agent mode, using the actual absolute path:

```text
/extension install D:\code\jiuwen-plugin\jiuwenswarm_8175\webui_workbench_v2.zip
/extension query webui.workbench-v2
```

If an older package with this ID is active, run `/extension uninstall webui.workbench-v2` first, then install the new ZIP and query it. Installation does not replace an active package. The current ZIP is version 1.0.9 and requires the updated Gateway, AgentServer and Web frontend. Replacing only the ZIP or refreshing an older frontend does not add the new components or session file API.

Use a Unix absolute path on Linux/macOS, such as `/home/me/work/webui_workbench_v2.zip`. These are Web commands, not shell commands. Successful installation activates the package; no enable command is needed. The response shows the actual installed directory. Startup loads installed copies and does not scan source `examples/`. Management commands work in a new empty conversation, but rendering still requires a real session.

After installation, enter an existing real conversation, then refresh or re-enter it so the Host sends `ui.ready`. The first ready generates demo trees immediately. Later ready events restore the active extension's task state. Ready for the file instance reads the root again and clears the preview; ready for the webpage instance republishes its Iframe.

## Open and interact

| Slot | Content and entry |
| --- | --- |
| `application.menu` → `application.menu.main` | Click “交互工作台 / Workbench” in the sidebar to open the login shell; both use instance `workbench` |
| `task.function` → `task.function.main` | Click “任务检查 / Inspect tasks” in the task area; both use `task-inspector` |
| `application.entry` | Fullscreen entry on the first browser visit; the plugin decides whether to show and when to finish |
| `application.settings` | “自动摘要 / Auto summary” in the settings content area; scoped to this conversation |
| `conversation.input.above` | Current filters and latest operation result |
| `conversation.input.below` | Compact operation count |
| `conversation.panel.right` | Workspace files and Web preview tabs use Panel and Iframe roots, respectively, mutually exclusive with native content |
| `conversation.turn.content.after` | A completed assistant round's state snapshot |
| `conversation.turn.footer.actions` | “Refresh round” and “Round details” buttons |
| `conversation.header.title.after` | Workbench status Tag |
| `conversation.header.actions` | Refresh the current conversation's demo state |

The login shell contains only an icon and two blue buttons labeled “登录” and “退出” in one row. The plugin function login_page() declares the vertical stack, horizontal and vertical centering, and spacing using existing Box, Image and Button components. Neither button declares an event, sends a request or maintains login state. The page has no form, tabs, explanatory text or demo switches.

The task-area Inspect tasks page retains filters and results. Select supports search and clear, TreeSelect supports project selection and clear, and Enter applies completed input.

Round slots use the actual `requestId` as `instance_key`. The first mount saves that round's snapshot; later ready events restore it. Changes in other rounds or sessions do not overwrite it. “Refresh round” explicitly copies the current filter summary into that round. These slots may remain absent before completion, without a `requestId`, or when the host hides footer metadata.

The page host only supplies generic responsive padding and a flexible column for the instance. The plugin root Box determines whether to fill that space, how to center content and how to arrange it. The frontend has no login-specific branch. To test version 1.0.9, use the Web frontend build with the updated instance container, then install this ZIP.

## Fullscreen entry on the first visit

Version 1.0.9 moves application.entry from the empty chat into the application root, covering the entire application viewport. On the first visit the host sends ui.ready with `{reason: "first_visit"}`. The plugin declares a centered icon, welcome text and a “开始使用 / Get started” button using its own Box layout. The button publishes clear through ops.ui.render. A plugin may also clear immediately on ready to skip the entry. The host contains no welcome content or login business logic.

The localStorage key `workswarm.webui.entry.visited` records the first trigger for this browser and site. It is written before the request, so refreshing during the welcome page, reopening the site, creating a conversation or changing accounts does not trigger it again. A missing contribution or a failed request also consumes this first opportunity. Without valid content the normal application stays visible. The background cannot receive interaction while the entry is shown; disconnection ends it without automatically reopening it.

The entry uses an empty session_id and a separate application_id. Gateway supplies trusted connection routing over the existing WebUI channel. No chat session or HTML/JS transfer endpoint is created. Update Gateway, AgentServer and the frontend together with this package. See [S_04](../../docs/specs/S_04_webui-application-entry.md) for the protocol and lifecycle.

For manual testing, install the package before visiting the updated frontend. If a first-visit record already exists, remove only the `workswarm.webui.entry.visited` localStorage item in browser developer tools, or run the following and refresh. Other site data can stay in place:

```javascript
localStorage.removeItem('workswarm.webui.entry.visited');
```

Check the first display, Get started clearing the entry, refresh during display, reopening and conversation switching. Entry content and actions must remain isolated between browser connections. Only source and ZIP checks were performed; automated tests, builds and browser integration were not run.

## Right panel update

The updated ZIP is version 1.0.9. The package declares two right-side contributions: `workbench.details` publishes a `Panel` root with instance `workbench`; `workbench.iframe` publishes an `Iframe` root with instance `iframe-preview`. A right-side ready event without an instance publishes both independent instances. Ready with a valid instance restores only that member. The host creates tabs from their root titles.

The file tab uses `Panel(title, children)` in one tree. The host supplies body styles, and a `Box` places `FileTree` and `FilePreview` side by side. The plugin supplies directory data, structured statuses and preview references. File components manage expansion, selection, loading and error messages; the plugin stores neither interaction state nor message text. Both file components are registered independently for reuse by other plugins. See [F_07](../../docs/features/F_07_ui-contributions.md#2026-10-27-会话文件组件实施方案).

Expanding the tool panel preserves the native selection by default. Click “工作区文件 / Workspace files” or “网页预览 / Web preview” to show that plugin instance, or a native entry to return. Plugin entries and bodies appear only in expanded and fullscreen modes. The collapsed floating panel shows only native content, preserving the plugin selection for the next expansion. Expanding a specific native section from the floating panel instead opens that native page. Plugin updates do not switch the current view. Removing the selected contribution restores native content. Multiple entries can scroll horizontally. Existing tool-panel visibility rules still apply.

Single Agent chat and code sessions can browse files. Project sessions show their bound project root; projectless sessions show the bound task root including `work/` and `outputs/`. Team mode displays an unsupported notice in this version. Missing bindings display an unavailable state. Browsing never creates a workspace or falls back to a global directory.

When the file instance receives `ui.ready`, it reads the root directory and clears the preview. Expanding a directory that has not been loaded requests its children. Limits are 100 entries per directory, 512 entries in total, 8 levels and the frame size budget; the component displays truncation notices. The component keeps at most 100 expanded directories and sends the complete requested `expandedKeys` in `ui.change`. The plugin maps these keys to paths in the current directory data and calls `ops.session.files`. Loaded directories expand and collapse locally without another request, and collapsing preserves the preview. If a later directory request fails, the plugin keeps the latest valid directory data and marks the read failure. The component collapses directories that remain unloaded so they can be expanded again to retry. File creation and deletion are not pushed automatically. Re-entering the conversation so the file instance receives ready reads the root again. There is no file refresh button, and the conversation header's Workbench refresh does not read files.

This version previews UTF-8 text, code, Markdown and common images, limited to 1 MiB for text and 4 MiB for images, also subject to the message transfer budget. HTML is displayed as code. Other formats and binary content show an unsupported notice. Selecting a file only calls `ops.session.preview` for a reference; it does not reload the directory tree. The preview component fetches content through a reference scoped to the current session. Content is never embedded in render trees. Frontend components display states for empty files, removed files, size limits and read errors. Editing, saving and writing files are outside this version.

## Web preview tab

The webpage instance uses schema v2 `Iframe`, a right-side root type alongside `Panel`. Iframe can only be the render tree root; it cannot be nested in Panel, Box or another component. It accepts no children, events or srcdoc. The example sends only a title and URL:

```json
{"component":"Iframe","props":{"title":"网页预览 / Web preview","src":"https://example.com"}}
```

The example loads `https://example.com` directly, without a demo asset in the platform's `public/` directory or a postMessage bridge. File operations and Workbench filter updates do not republish the webpage instance. Version 1.0.7 switches from Baidu to example.com at the user's request to test an external page. The plugin sends src and title through the existing render channel; the browser requests src directly. The plugin does not transfer HTML or bundled JavaScript, and installing the ZIP does not expose webpage resources in the plugin directory.

src accepts absolute HTTP(S) URLs, `//host`, `/path` and ordinary paths relative to the Web host; it is not restricted to localhost. The platform rejects URL credentials, backslashes, ASCII control characters and ambiguous forms such as `http:foo` or `///host`. Browser policies govern mixed content, such as HTTP addresses embedded in an HTTPS page.

The host fixes `sandbox="allow-scripts allow-forms"`. Plugins cannot enable same-origin access, popups or top-level navigation. External sites must permit embedding; `X-Frame-Options` or CSP `frame-ancestors` may prevent display. Pages that depend on parent DOM access, same-origin access or same-origin storage may also fail in this sandbox. Confirm compatibility before replacing src.

## Reset, edit and uninstall

“Reset” restores the current session's filters and demo switches while preserving operation counts and saved round snapshots. Plugin data lives in the extension activation's memory. Restarting the process or successfully reloading the package initializes it again. The file tree's expansion and selection belong to the frontend component; the plugin only caches directory and preview results returned by the platform.

Edit the installed copy identified in the installation response, then run:

```text
/extension reload webui.workbench-v2
/extension query webui.workbench-v2
```

Reload reads the installed copy, not the original ZIP. After editing repository sources, copy the changes into that installed copy or rebuild and install the ZIP. Remount the page after a successful reload to generate the new activation's trees. To uninstall:

```text
/extension uninstall webui.workbench-v2
/extension query webui.workbench-v2
```

Uninstall uses platform cleanup and revision handling. The current management entry has no separate enable/disable command.

## Protocol and validation limits

Manifest `schemaVersion: 1`, `ui.apiVersion: 1` and frame `schema_version: 2` describe separate formats. This package uses v2 `events['ui.click']` and other declared events. Business arguments come from the declared action's `args`; control values come from normalized event `data`. A dispatch response is a receipt, never a replacement tree. See [F_08](../../docs/features/F_08_frontend-render.md) for the shared version and validation strategy.

`application.entry` uses an application target without a chat session. Other slots still require a real `session_id`; menus and settings do not yet support rendering without one. The right side uses two stable instances with separate events and updates; other single slots each have one instance. The welcome entry selects one plugin by contribution order, package ID and contribution ID. Other single slots select the latest update. `message.assistant.meta` and the old `applicationPlugins` entry are outside these thirteen targets.

The user will test this package manually. Suggested order: install and query → open a real session → open Workbench → change controls and inspect auxiliary slots → check another session and two real rounds → remount and restore → reload, uninstall and reconnect. Compare settings alignment, typography, grouping and narrow layouts with existing settings modules. Confirm that the sidebar login page shows only the centered icon and two blue buttons, and clicking them sends no business request. Check its narrow layout, and check Chinese input, empty results and both themes in task inspection.

For the file panel, check project and projectless chat/code sessions, empty directories, text/code/Markdown/images, empty files and unsupported formats. Switch files and sessions quickly, check the read failure state after deleting a file, then re-enter the conversation to check the root directory. Check Unicode names, spaces, nested paths, narrow scrolling, collapsed/expanded/fullscreen behavior and uninstall cleanup. Confirm that there is no file refresh button and Workbench refresh in the conversation header does not read files. No automated tests, builds, services or browser integration were added or run for this change; these manual checks remain to be performed.

For the webpage tab, check example.com page loading, narrow layouts, switching between native/file/webpage tabs and session isolation. Change files or Workbench filters and confirm that unrelated updates do not reload the page. Embedding policies and sandbox compatibility require browser validation.

The initial targeted pytest run passed 7 tests with actual package loading and WebUI Runtime using a recording transport. Tests were not rerun after subsequent fixes. Real network round trips and browser layouts remain unverified. Existing results and their scope are recorded in the [implementation and acceptance record](../../../../fontenddesign/03_工作台插件实施验收记录.md).

## Rebuild after editing sources

To rebuild the ZIP after source edits, run this Python script from the repository root. The ZIP root contains `manifest.yaml` and `webui_workbench_v2/` directly:

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
