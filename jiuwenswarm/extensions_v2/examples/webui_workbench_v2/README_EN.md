# Plugin Workbench

[中文](README.md)

`webui.workbench-v2` demonstrates 12 registered WebUI slots and the catalog components `Input`, `Select`, `TreeSelect`, `Slider`, `Switch`, `Button`, `Card`, `Tag`, `Empty`, `Image`, `Iframe`, `Box` and `Text`. The sidebar main page is a presentation-only login shell, task inspection uses fixed data, and the right-panel web tab loads example.com directly. The plugin keeps filter state and publishes complete trees downstream. It does not call models or external business APIs.

This copy is trimmed for the current branch: the reference branch's fullscreen `application.entry` welcome page and the `FileTree`/`FilePreview` workspace tab are removed (both capabilities are deferred on this branch), so `conversation.panel.right` only serves the `Iframe` web-preview instance.

## Install and enable

Requires a Gateway, AgentServer and web frontend that include the current WebUI protocol and assembly changes. To rebuild the ZIP from source, run the following script at the repository root:

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

Put the ZIP where the AgentServer host can read it, then run in the web single-agent input, for example:

```text
/extension install D:\path\to\webui_workbench_v2.zip
/extension query webui.workbench-v2
```

If an older package with the same ID exists, run `/extension uninstall webui.workbench-v2` first, then install the new ZIP and query again. A successful install is active immediately; no separate enable step is needed. The service only loads the install directory, it does not scan `examples/` in the source tree. Management commands work in an empty new conversation, but plugin rendering still requires a real session.

After installing, open an existing real session and refresh the page or re-enter the session so the host sends `ui.ready`. The first ready builds the demo trees; later ready events restore the current activation's task state.

## Access and interaction

| Location | Content and access |
| --- | --- |
| `application.menu` → `application.menu.main` | Click "交互工作台 / Workbench" in the sidebar to open the login shell; both instances use `workbench` |
| `task.function` → `task.function.main` | Click "任务检查 / Inspect tasks"; both instances use `task-inspector` |
| `application.settings` | The settings area shows the "自动摘要 / Auto summary" switch, scoped to the current session |
| `conversation.input.above` | Filter summary and the latest operation result |
| `conversation.input.below` | Compact operation counter |
| `conversation.panel.right` | The `Iframe` web-preview instance `iframe-preview`, mutually exclusive with native content |
| `conversation.turn.content.after` | Status snapshot of finished assistant turns |
| `conversation.turn.footer.actions` | "刷新本轮 / Refresh round" and "本轮详情 / Round details" |
| `conversation.header.title.after` | Workbench status Tag |
| `conversation.header.actions` | Button that refreshes the workbench for the current session |

The login page contains one icon and a "登录 / 退出" button pair. The buttons declare no events, call no APIs and keep no login state.

Task inspection provides filters and results: `Input` supports CJK composition and enter-to-apply, `Select` supports search, select and clear, `TreeSelect` supports project selection and clear, `Slider` controls the score threshold (step 5), `Switch` toggles auto summary, and `Button` triggers apply and reset.

The turn slots use the actual `requestId` as `instance_key`. The first mount stores that round's snapshot; later ready events restore it. Changes from other rounds or sessions never overwrite it; only "刷新本轮" writes the current filter summary into that round.

## Reset, reload and uninstall

"重置 / Reset" restores the session's filters and demo switches while keeping the operation counter and turn snapshots. Plugin data lives only in the extension activation's memory; a process restart or successful reload reinitializes it.

After editing the installed copy, run:

```text
/extension reload webui.workbench-v2
/extension query webui.workbench-v2
```

reload reads the installed copy, not the original ZIP; changes in the repository source must be synced to the installed copy or repackaged. To uninstall:

```text
/extension uninstall webui.workbench-v2
/extension query webui.workbench-v2
```

## Protocol and verification boundaries

manifest `schemaVersion: 1`, `ui.apiVersion: 1` and the render frame `schema_version: 2` mean different things. The package uses v2 events such as `events['ui.click']`; business parameters live in the declared action's `args`, control values in the normalized event `data`. A dispatch response is only a receipt and never a new tree. See [F_08](../../docs/features/F_08_frontend-render.md) for the general policy.

Every location uses a real `session_id`; menus and settings do not support session-less rendering yet. The right panel uses the stable instance `iframe-preview` and events are isolated per instance; every other single slot creates one instance for this package. `message.assistant.meta` and the legacy `applicationPlugins` are not targets of this package.

The web instance uses `Iframe`, which may only be a render-tree root; it cannot nest inside `Panel`, `Box` or other components and accepts no children, events or srcdoc. The demo only sends a title and URL:

```json
{"component":"Iframe","props":{"title":"网页预览 / Web preview","src":"https://example.com"}}
```

The host pins `sandbox="allow-scripts allow-forms"`; plugins cannot relax same-origin permissions, popups or top-level navigation. External sites must also allow embedding; `X-Frame-Options` or CSP `frame-ancestors` may block display.

## Suggested manual checks

Install and query → first ready in a real session → menu main page → control operations and side-area echo (CJK input, Select search/clear, TreeSelect, Slider, Switch) → second session and two real turn snapshots → right-panel example.com tab → remount restore → reload, uninstall and reconnect.
