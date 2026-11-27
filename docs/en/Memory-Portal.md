# Memory Portal

Use **Settings → Memory → Memory management** to control only the Portal entry. It is disabled by default.
Memory operations in Work, Code and Team modes retain their existing configuration.
When enabled, open **Memory** from the left navigation or menu. Disabling hides the entry and closes the embedded page;
existing memories are retained.

## Default Personal Edition connection

When the installed JiuwenMemory supports the Portal, it uses the same built-in HTTP listener and store as the memory plugin, even when the legacy built-in mode is set to `sdk`.
The locked JiuwenMemory `0.2.1` supports memory reads and writes over that listener, but does not provide the Portal or its model settings.
No separate memory service, API key configuration for the Portal, or iframe origin list is needed. The browser must be able to reach
the loopback listener on the machine running Swarm. If the listener is unavailable, the Portal does not fall back to a separate SDK store.
The page shows personal memories permitted by Memory; it does not bypass project or team access rules.

The following configuration explicitly enables the management page:

```yaml
memory:
  builtin:
    provider: jiuwen
    jiuwen:
      portal:
        enabled: true
```

The switch saves `memory.builtin.jiuwen.portal.enabled`. Disabling hides the entry, unmounts the page
and stops issuing new launch links. Recall, synchronization, tools and session-end extraction remain unchanged.
Saving only this setting does not reload Agents or stop parked teams. Disabling retains the existing Portal instance;
reopening reuses the service and requests a fresh launch link. The Portal does not own the plugin listener. For legacy providers,
connection changes or Swarm shutdown release owned providers; borrowed conversation providers keep running.
Existing browser authorization sessions retain Memory's normal lifetime. Enterprise Edition continues to use its configured provider.

The page obtains a fresh authorized URL on entry or reconnect. URLs are not persisted. Memory owns the inner menus,
data permissions and model management. Swarm does not duplicate these features.

In Personal Edition, **Model settings** changes the supported LLMs used by the built-in memory service, not the Swarm chat model.
Saved overrides apply to subsequent model calls and survive restarts. They are stored in Memory's local configuration database,
not written back to Swarm's YAML. Restoring a field removes its override and uses the startup value again.

## Optional HTTP deployment

Set `memory.builtin.jiuwen.mode: http` and provide `base_url`, `api_key`, `identity_org` and `identity_user` in the config file.
A nonempty `api_key` takes precedence and is decrypted when possible; decryption failure preserves the original value
for compatibility with existing plaintext settings. A missing or empty value uses `JIUWEN_MEMORY_API_KEY` as plaintext.
Both Swarm and the browser must reach the service. This Portal connection does not change the Personal Edition plugin's own store.
No `http.portal_frame_origins` setting is needed; browser authorization still requires the launch URL issued by Memory.

Saved page-switch changes synchronize both navigation and the visible settings switch across tabs on the same browser origin,
without rebuilding teams or saving the configuration again.

Do not share Portal launch URLs. The iframe sandbox and no-referrer policy do not replace server-side clickjacking protection.
Deployments requiring an embedding restriction should set CSP `frame-ancestors` on the Memory server or reverse proxy,
allowing only trusted Swarm origins.

Saving the page switch does not wait for Portal initialization. Connection changes wait at most one second for the service lock.
If that wait times out, normal configuration reload continues; the next Portal open or reconnect reconciles the latest settings.
