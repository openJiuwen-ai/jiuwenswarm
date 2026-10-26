# Connect Python Gateway to ADX

Configure ADX access in the Gateway using the example below and select `gateway.agent_client.type: adx_router`. This is a deployment-wide backend switch. Existing Jiuwen authentication, client protocols, configuration and Cron interfaces remain in use. Restore the original backend configuration to switch back. This adapter currently supports Web, TUI and Cron; other channels are not adapted.

```yaml
gateway:
  agent_client:
    type: adx_router
  adx:
    ingress_url: https://adx.example:8443
    tenant: appliance
    template: jiuwen
    version: appliance-v1
    token_env: ADX_PLATFORM_API_KEY
    service_port: 18092
    connect_timeout_seconds: 180
    max_connections: 128
    parameters:
      model_api_base: http://model.internal:8080/v1
```

Set `ADX_PLATFORM_API_KEY` in the process environment. Its ADX tenant must match `tenant`; this platform credential is separate from Jiuwen user identities. The HTTPS Ingress certificate must be trusted by the system. Keep real credentials out of configuration examples.

File HTTP requests use the WebChannel's existing business authentication callback (`set_handshake_auth`). The callback must return a trusted Jiuwen business `user_id`. Missing authentication callbacks or identities return 401; a claimed user that differs from the authenticated identity returns 403. ADX platform credentials do not establish a business identity, and this adapter does not use ADX IAM.

## Prepare ADX and AgentServer

Publish the configured version through the public ADX Template API. Use an AgentServer image available to every node and declare `service: [{protocol: ws, port: 18092}]`, adjusting the port to the image. Set AgentServer `sandbox.enabled=false` so file and Shell tools execute inside the current ADX sandbox.

Declare persistent mounts in the Template, for example:

```json
{
  "env": {"API_BASE": "${model_api_base}"},
  "mounts": [{
    "type": "bind",
    "host_path": "/mnt/workspaces/${workspace}",
    "target": "/home/agentos",
    "options": ["rw"],
    "create_if_missing": true
  }]
}
```

This is a Template fragment; a complete request also requires name, version, image, isolation_runtime and resources. API_BASE illustrates rendering; the image startup must consume the actual variables you configure. Template and parameters supply startup settings without a private Activator launch snapshot. Manage workspace parent directories at deployment time. For shared storage across nodes, mount a distributed filesystem at the same absolute path beforehand. The built-in ADX Jiuwen module is not required.

## First access and rebuilding

Gateway maps an existing trusted Jiuwen `user_id` to a stable Binding and derives `workspace` automatically. Configuration cannot override this parameter. Concurrent requests for a user share a backend connection; separate users have separate connections and Bindings.

Once all frontends for a user disconnect, Gateway waits 60 seconds before attempting to close an idle backend connection. A check every 30 seconds also reclaims connections idle for 600 seconds whose frontends are offline. At capacity, the oldest eligible connection is reclaimed first. Online users and in-flight requests are protected; if none can be reclaimed, the request fails with `ADX_CAPACITY_EXCEEDED`. Reclamation closes only the WebSocket, leaving the Binding and sandbox intact.

Gateway opens `/agent/ws` directly with platform authentication and `X-ADX-Parameters`, using URL form encoding without JSON or Base64 wrapping. No separate Activator URL or prepare, resolve or activate call is required.

Only a missing Binding causes ADX to parse parameters, render the Template and atomically create the Binding. Existing Bindings ignore subsequent initialization headers entirely; configuration edits do not replace stored parameters. To apply new startup parameters, delete the Binding through the existing rebuild/delete entrypoint and access it again. Persistent workspace data is independent of the Binding. Concurrent first requests reuse the parameters that won the atomic creation.

Generic parameters are currently stored as plaintext ADX Binding metadata. Do not pass production model secrets through this configuration; an unauthenticated test model can be used at this stage. Future business credential protection belongs to Jiuwen.

## Files and failures

Files, attachments and Cron retain their Gateway interfaces. The adapter selects the current user's Binding, queries its Environment through the public Binding API, and uses generic ADX forwarding. A file transfer pins its target and aborts when rebuilding or target changes; callers may start a new operation.

Directory creation calls ADX `file.mkdir` directly and inherits its recursive parent-directory creation.

File HTTP requests return 400 for invalid parameters and 502 when the ADX connection or Binding lookup is unavailable, preserving a specific error code.

In ADX mode, Gateway keeps its existing periodic health-check workflow. For now, `adx_router` returns a mocked success without contacting Ingress or AgentServer or creating a Binding, connection or sandbox; this response does not indicate actual backend readiness. Global AgentServer prewarming remains disabled. Configure sandbox readiness probes for the actual AgentServer in the ADX Template.

Streaming retains the E2A message protocol. Disconnects fail pending requests. Reconnection preserves ADX authentication and initialization headers. Business requests already sent are not replayed automatically, preventing duplicate messages or tool execution. Temporary cold-start handshake failures may be retried within the connection deadline.

For troubleshooting, check the Template version, platform credential tenant, certificate trust, model network ACL and AgentServer WS port, then inspect Gateway, ADX and sandbox logs. Do not log authentication headers or sensitive parameters.
