# Plugin Workbench

[中文](README.md)

`webui.workbench-v2` demonstrates 12 registered WebUI slots (13 contributions in total, with two instances in the right panel), covering catalog components such as `FileTree`, `FilePreview`, `Panel`, `Iframe`, `Switch`, `Button`, `Tag`, `Box`, and `Text`. The sidebar main page and task inspection main page use `Iframe` roots, while the right panel provides both a workspace files tab (`Panel` root combining `FileTree` and `FilePreview`) and a web preview tab (`Iframe` root). The plugin stores session state and platform file results, then publishes complete render trees. File components handle expansion, selection, and status messages locally. The demo calls no model or external business API.

## Install and activate

Run Gateway, AgentServer, and the Web frontend with this branch's WebUI protocol and runtime assembly. To build or deploy the package from source, run `python scripts/install_workbench_plugin.py` (or `--zip-only` to only build `webui_workbench_v2.zip`) from the repository root.

Place the generated ZIP where the AgentServer machine can read it, then run these commands in the Web input in single-agent mode:

```text
/extension install /path/to/webui_workbench_v2.zip
/extension query webui.workbench-v2
```

If an older package with the same ID is active, run `/extension uninstall webui.workbench-v2` first, then install the new ZIP and query it. Successful installation activates the package immediately. Startup loads installed copies and does not scan `examples/` in the source tree. Management commands work in a new empty conversation, but rendering requires a real session.

After installation, open an existing real conversation and refresh or re-enter it so the Host sends `ui.ready`. The first ready builds the demo trees; later ready events restore the active extension's state. Ready for the file instance reads the root directory again and clears the preview; ready for the webpage instance republishes its Iframe.

## Open and interact

| Slot | Content and entry |
| --- | --- |
| `application.menu` → `application.menu.main` | Click "交互工作台 / Workbench" in the sidebar to open the main page (`Iframe` root); both use instance `workbench` |
| `task.function` → `task.function.main` | Click "任务检查 / Inspect tasks" in the task area to open the task page (`Iframe` root); both use `task-inspector` |
| `application.settings` | "自动摘要 / Auto summary" switch in the settings content area, scoped to the current conversation |
| `conversation.input.above` | Current filter summary and latest operation result |
| `conversation.input.below` | Compact operation count |
| `conversation.panel.right` | "Workspace files" (`Panel` root, instance `workbench`) and "Web preview" (`Iframe` root, instance `iframe-preview`) tabs, mutually exclusive with native content |
| `conversation.turn.content.after` | State snapshot for a completed assistant round |
| `conversation.turn.footer.actions` | "Refresh round" and "Round details" buttons |
| `conversation.header.title.after` | Workbench status Tag |
| `conversation.header.actions` | Button that refreshes the current conversation's workbench state |

Turn slots use the actual `requestId` as `instance_key`. The first mount stores that round's snapshot; later ready events restore it. Changes in other rounds or sessions do not overwrite it; clicking "Refresh round" copies the current filter summary into that round.

## Right panel: workspace files and web preview

The package declares two right-side contributions: `workbench.details` publishes a `Panel` root with instance `workbench`; `workbench.iframe` publishes an `Iframe` root with instance `iframe-preview`. A right-side ready event without an instance publishes both independent instances; ready with a valid instance restores only that member. The host creates tabs from their root titles.

The file tab uses `Panel(title, children)` and places `FileTree` and `FilePreview` side by side inside a `Box`. Both file components are registered independently for reuse by other plugins. See [F_07](../../docs/features/F_07_ui-contributions.md#44-文件能力目录与预览) for the component and platform API details.

Single-agent chat and code sessions can browse files. Project sessions use their bound project root; projectless sessions use the bound task root (including `work/` and `outputs/`). Team mode returns an unsupported status, and missing bindings return an unavailable status.

When the file instance receives `ui.ready`, it calls `ops.session.files` to read the root directory and clears the preview. Expanding a directory sends `expandedKeys` via `ui.change`; the plugin maps keys to paths in the current snapshot and calls `ops.session.files` again (up to 100 entries per directory, 512 entries total, 8 levels, and 96 KiB). Selecting a file calls `ops.session.read_file` to read UTF-8 text up to 16 KiB and passes the result to `FilePreview`; binary files, images, and oversized files display their corresponding status notices.

The webpage instance uses an `Iframe` root, which is only permitted at the root of slots that declare it (`conversation.panel.right`, `application.menu.main`, `task.function.main`). It cannot be nested inside `Panel`, `Box`, or other components, and accepts no children, events, or srcdoc. The host pins `sandbox="allow-scripts allow-forms"`.

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

All slots require a real `session_id`. The right panel uses two stable instances with isolated events and updates; other single slots select the latest update. `message.assistant.meta` and `application.entry` are outside this package's targets.

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
