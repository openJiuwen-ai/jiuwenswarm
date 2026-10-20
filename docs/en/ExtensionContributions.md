# Extension contributions

An extension can add a channel, an AgentServer adapter, or agent behavior without
changing the host package. Put `extension.yaml` and `extension.py` in a directory
under `~/.jiuwenswarm/application_plugins`. The loader calls the asynchronous
`register_extensions(registry)` function in `extension.py`.

Set `requires_transport: false` in the manifest when both Gateway and
AgentServer need the extension. A transport extension runs in Gateway only.

## Channels and adapters

Call `registry.register_channel_spec(ChannelSpec(...))` in
`register_extensions(registry)` to contribute a channel. Import `ChannelSpec`
from `jiuwenswarm.extensions.channel_contributions`. Annotate `registry` with
`ContributionRegistry` from `jiuwenswarm.extensions.sdk.contributions`.
Set `channel` to its ID, `factory` to its constructor, and
`delivery` to its delivery-target factory. The optional `check_relay` callback
can reject an unreachable heartbeat relay during startup.

Set `delivery` to allow cron jobs to target a new channel ID. Set
`shared_im_input=True` if the connector uses the shared IM session-input path.
The extension must load in both Gateway and AgentServer for cron tools to see
its channel ID (`requires_transport: false`).

To replace a built-in channel, set `replaces` to its channel ID and add
`channels.<id>` to `extensions.allow_overrides` in the host configuration.
The channel factory does not run when either declaration is missing.

Call `registry.register_gateway_adapter(adapter)` in the same entry function
to add an AgentServer adapter. It can handle a contributed channel without
changing the built-in adapter table.
Registration is process-wide. Resetting the registry does not withdraw
contributions.
Use `registry.register_cron_hooks(channel, hooks)` for channel-specific cron
checks and run metadata. The hook type is `sdk.cron_hooks.CronHooks`.

## Host settings and paths

Use `jiuwenswarm.extensions.sdk.host_config` to read host configuration and
configured models, or to build host-compatible reasoning model requests. Use
`jiuwenswarm.extensions.sdk.workspace_paths` for host directories,
`jiuwenswarm.extensions.sdk.host_types` for message types, and
`jiuwenswarm.extensions.sdk.memory` for memory services. Extensions manage
their own configuration separately.

## Agent behavior

`registry.register_agent_plugin(mount)` adds a plugin mount. The mount receives
`AgentPluginServices`, including the agent, plugin loader, runtime tool
registration, and request context registration.

The registry also accepts turn-envelope fields, final-record filters,
agent-input transforms, and a Skill inventory. Each registration method has
a typed callback in `jiuwenswarm.extensions.sdk.contribution_callbacks`.
Turn fields cannot replace host fields. Final filters affect stored assistant
finals. They do not change the response sent to the channel.
