# System plugin settings

System plugins declare settings menus, sections and items through `application.settings`. Once a plugin is installed and active, its settings appear at the declared locations.

Settings no longer includes a built-in Extensions menu or startup splash settings page. Plugins can provide their own menus or add items to existing settings sections.
Plugin splash playback, previews and theme backgrounds continue to use their existing entry points.

A plugin menu without an ordering anchor is appended after existing menus. Older declarations using `after: "extensions"` also append under the missing-anchor rule, so their menus and settings remain available.

See the [S_18 settings location specification](../../jiuwenswarm/extensions_v2/docs/specs/S_18_settings-location.md) for declaration and placement rules.

## System Plugin Recovery

Recovery restores installed plugins' enabled states to a selected record, then restarts AgentServer, its owned Worker, and Gateway. Active tasks are interrupted. Removed packages and plugins' file or network side effects are outside the scope of recovery.

- When the main UI is available, open **Settings → System Plugin Recovery**. The current chat URL stays in the address bar.
- If the main UI freezes or shows a blank screen, open `/system-plugin-recovery` on the Web address in a new tab. The Web service connects the standalone page to the recovery process without loading the chat frontend or connecting to AgentServer or Gateway.
- If the Web service is also unavailable, an administrator can open the `url` from `system_plugin_recovery_endpoint.json` in a browser on the deployment host, provided the recovery process and launcher are still running. The extension data directory is resolved from `extensions.data_dir`, then `EXTENSION_DATA_DIR`, and defaults to `~/.jiuwenswarm/extension`. Keep the URL private because it contains the local recovery token. A remote operator can use SSH to forward the same local port to the recovery port on the deployment host's `127.0.0.1`.

With no recovery password configured, users can open the Personal Edition Web service by IP address or domain name to view restore points, preview changes, and restore plugin state. Clients that can reach the Web recovery endpoint can request a restore and backend restart, so restrict access to trusted networks or enable authentication.

To require authentication for remote recovery, set `JIUWENSWARM_REMOTE_RECOVERY_PASSWORD` for the Web service, bind the service to loopback, and place an HTTPS reverse proxy on the same host. Configure the proxy to preserve the public `Host` and overwrite `X-Forwarded-Proto` with `https`. Once the password is set, all remote viewing and restore requests require authentication through that HTTPS endpoint. Open `/system-plugin-recovery` and sign in with HTTP Basic username `recovery` and the configured password. Administrators can also use SSH to forward the same local port to the Web service's loopback port and open the local recovery page.
