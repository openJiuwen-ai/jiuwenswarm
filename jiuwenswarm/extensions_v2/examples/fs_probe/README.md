# FS Probe

Demonstrates `ops.fs.*` from a WebUI extension package.

- The panel is declared in `manifest.yaml` on `conversation.panel.right`.
- A `context.usage.updated` handler scans the session working directory with
  `fs.list`, `fs.exists`, `fs.read`, `fs.stat`, and `fs.ancestors`.
  `fs.ancestors` stops at the host's `project_root` (or the session directory
  when none is reported), so the scan stays inside the workspace.
- The panel renders the real results through `ops.ui.render`.
- `Rescan` repeats the scan; `Write marker` calls `fs.write` and stores
  `fs-probe-marker.md` in the session directory, then rescans so the new file
  appears in the tree and preview.
- `ui.ready` scans the workspace when the session has no snapshot yet, and
  otherwise restores the last snapshot so a page refresh keeps the panel.
- Expanding a directory lists one level through `fs.list`; the tree reports
  open and closed directories through `ui.change`.

Install by copying this directory to
`~/.jiuwenswarm/extension_pkgs/example.fs-probe/1.0.0` and restarting the
AgentServer.
