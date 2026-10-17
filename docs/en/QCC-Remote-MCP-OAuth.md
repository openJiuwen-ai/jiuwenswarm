# QCC remote MCP OAuth (desktop)

The installed `qcc-company` connector can use browser authorization instead of copying an API Key. API Key authentication remains available. Existing API Key connections are unchanged; disconnect and reconnect to choose OAuth.

## Scope and operation

This first implementation is deliberately limited to the installed `qcc-company` package pointing to `https://agent.qcc.com/mcp/company/stream`. It does not replace the distributed connector archive or enable arbitrary OAuth providers. A source checkout whose marketplace lacks that package must install the official connector package first.

1. Connect QCC from the connector marketplace and choose browser authorization.
2. WorkSwarm discovers the trusted QCC metadata, registers a public client, and opens the browser. Sign in and authorize on `agent.qcc.com`.
3. A temporary `127.0.0.1` callback listener verifies state and exchanges the code with PKCE S256. The flow expires after five minutes.
4. WorkSwarm probes the MCP connection before showing Connected. Closing the modal or navigating away cancels unfinished authorization.
5. Tokens refresh automatically. Disconnect attempts remote revocation and removes local authorization even if revocation is unavailable. Submitting an API Key explicitly switches back to legacy authentication.

Access and refresh tokens stay in the existing encrypted backend CredentialStore. Renderer responses and `state.json` contain no tokens. Refresh-token rotation uses a cross-process file lock. Invalid grants require explicit reconnection. OAuth HTTP requests cannot follow redirects or send credentials to another resource. Other MCP transports are unchanged.

The runtime uses the repository's pinned agent-core and MCP SDK APIs; no dependency version changes are required. The callback is local to the backend machine, so this flow is intended for the desktop/local backend, not a browser accessing a remote backend.

## Verification

Backend tests:

```sh
pytest tests/unit_tests/agentserver/mcp/test_remote_oauth.py tests/unit_tests/agentserver/mcp/test_remote_oauth_integration.py tests/unit_tests/agentserver/mcp/test_mcp_credential.py tests/unit_tests/agentserver/mcp/test_mcp_token_flow.py tests/unit_tests/agentserver/mcp/test_mcp_config_placeholder.py -o addopts='' --asyncio-mode=auto -o log_cli=false
```

Frontend (from `jiuwenswarm/channels/web/frontend`):

```sh
node --test tests/remoteOAuthModal.test.mjs tests/pendingConnectorFlow.test.mjs tests/i18nLocales.test.mjs
npm run build
```

Tests exercise real loopback callbacks and the pinned SDK's initialization, tool listing and tool calls against mocked HTTPS responses. They cover consent denial, incorrect state/Host, cancellation, token rotation, persistence, redirects, and API Key compatibility. Mocked providers do not establish production account compatibility.

Before release, verify in a packaged desktop build with a QCC test account: consent and real company query, restart, expiry/refresh, disconnect/revoke, API Key fallback, and modal layout in Chinese and English. Confirm that the distributed connector package and official release channel include the change. Upstream merge and a desktop release are separate steps.
