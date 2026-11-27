# Configuration extension probe

This AgentServer package verifies `config.describe`, `config.set`, and the three read-only `ops.config` methods.

The host contributes the Ops during `ops.build` and owns the public catalog, event types, default CORE handlers,
native configuration file and runtime reload. This package contributes USER subscriptions only: an RSI label,
a policy refusing to enable RSI, and the `/config_probe` command. It creates no Bus or SessionContext and has no UI slot.

ZIP the four files at the archive root. In a supported single-agent conversation run:

```text
/extension install "<absolute-local-path>/config-probe.zip"
/config_probe
```

The command returns `value`, `items`, and `snapshot`. The RSI row is labelled `RSI (config probe)`.
The snapshot exposes registered public fields only, without model secrets or arbitrary YAML data.
Try enabling RSI through the existing settings entry or a native `config.set` request with `{"rsi_enabled":true}`.
Expect `CONFIG_SET_DENIED` and no write for that request. Disabling RSI still continues to the native writer.

```text
/extension uninstall example.config-probe
```

Uninstall removes the command and policy, leaving the platform capabilities available. Automated acceptance uses
an isolated real YAML file, actual ZIP installation, command dispatch and ConfigAdapter, then independently reads
the saved file. It does not require a model or UI rendering.

The initial public fields are `preferred_language`, `react.context_engine_config.enabled`, `rsi.enabled`, and
`rea.enabled`. Reads support `effective` and `user`; `project`/`session` are unsupported. Enterprise language
remains owned by Gateway/user preferences and is excluded from this host-YAML catalog. A bound session is not
required for these global configuration reads inside a valid extension Handler.
