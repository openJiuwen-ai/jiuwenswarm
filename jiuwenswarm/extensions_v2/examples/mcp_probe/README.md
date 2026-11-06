# MCP Probe

Demonstrates `ops.mcp.*` from a WebUI extension package against a real MCP
server started from `mcp.json`, plus runtime registration through
`ops.mcp.register`.

- The panel is declared in `manifest.json` on `conversation.panel.right`.
- `mcp.json` declares a stdio server whose tools are `echo`, `add` and
  `server_info`. Loading the package only records the declaration; the first
  `Connect` opens the connection through `mcp.connect`.
- `Register (runtime)` calls `ops.mcp.register` with the same declaration under
  the name `probe-dynamic`, then connects by the returned `server_id`. Use it
  to verify runtime registration without reloading the package.
- `Connect (mcp.json)` lists the declared server tools and publishes the
  server to the calling agent when one is bound to the session. Clicking it
  again re-lists the tools.
- `Call echo` / `Call info` invoke `mcp.call` on the connected server and show
  the result. These calls do not touch the model tool list.
- `ui.ready` restores the last panel state so a page refresh keeps the view.

Install by copying this directory to
`~/.jiuwenswarm/extension_pkgs/example.mcp-probe/1.0.0` and restarting the
AgentServer. The `python` on the AgentServer `PATH` must provide the `mcp`
package.

Component props follow [S_17](../../docs/specs/S_17_webui-component-props.md): Button uses value and type / variant;
Text uses type / strong / code / ellipsis. Single-line truncation uses ellipsis; multiline truncation has been removed.
The result length limit remains in place. Copy updated sources into the installed package before restarting;
editing this repository does not update an existing installation.
