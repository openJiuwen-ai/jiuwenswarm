# Memory Portal

Use **Settings → Memory → Memory management** to control only the Portal entry. It is disabled by default.
Memory operations in Work, Code and Team modes retain their existing configuration.
When enabled, open **Memory** from the left navigation or menu. Disabling hides the entry and closes the embedded page;
existing memories are retained.

## Default SDK connection

JiuwenMemory SDK is the default. Configure the Swarm chat model; the existing assembly forwards model settings to memory.
The default hashing embedder works without an embedding service. No separate Memory HTTP service or iframe origin list is required.
Identity, project/team boundaries and document directories keep their existing rules. The shared Runtime holder is reused;
Memory allocates the Portal port. Opening the page without a conversation creates only a memory provider.

The following configuration explicitly enables the management page:

```yaml
memory:
  builtin:
    provider: jiuwen
    jiuwen:
      mode: sdk
      portal:
        enabled: true
      scope_id: __default__
      user_id: __default__
      agent_id: main_agent
      session_id: ""
      space: ""
```

The switch saves `memory.builtin.jiuwen.portal.enabled`. Disabling hides the entry, unmounts the page
and stops issuing new launch links. Recall, synchronization, tools and session-end extraction remain unchanged.
Saving only this setting does not reload Agents or stop parked teams. Disabling retains the existing Portal instance;
reopening reuses the service and requests a fresh launch link. Connection changes or Swarm shutdown release owned providers;
borrowed conversation providers keep running. Existing browser authorization sessions retain Memory's normal lifetime.

The page obtains a fresh authorized URL on entry or reconnect. URLs are not persisted. Memory owns the inner menus,
data permissions and model management. Swarm does not duplicate these features.

## Optional HTTP deployment

Set `memory.builtin.jiuwen.mode: http` and provide `base_url`, `api_key`, `identity_org` and `identity_user` in the config file.
A nonempty `api_key` takes precedence; a missing or empty value falls back to `JIUWEN_MEMORY_API_KEY`.
Both Swarm and the browser must reach the service. Swarm does not start a local Runtime in this mode.
No `http.portal_frame_origins` setting is needed; browser authorization still requires the launch URL issued by Memory.

Saved page-switch changes synchronize both navigation and the visible settings switch across tabs on the same browser origin,
without rebuilding teams or saving the configuration again.
