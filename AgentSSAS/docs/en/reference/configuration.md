# Configuration Reference

AgentSSAS configuration is carried by `AgentSSASConfig` (defined in `src/agent_ssas/core/framework/config/settings.py`). It is loaded from the `ssas` section of JiuwenSwarm's `config.yaml` and can be overridden by `SSAS_`-prefixed environment variables. The configuration is injected into each module (access adaptation, data preprocessing, pipeline, data modeling, threat analysis, storage, presentation, and the detection module manager).

Related documentation:

- [Switch Decision Policy](../how-to/switch-decision-policy.md): how to use `decision_policy`
- [HTTP API Reference](./http-api.md): runtime effects of the HTTP mode configuration
- [Detection Modules Reference](./detection-modules.md): relationship between the `modules` section and module.yaml

## Loading Order and Precedence

The loading order is:

1. **Defaults**: the dataclass defaults of the `AgentSSASConfig` fields.
2. **The `ssas` section of config.yaml**: `AgentSSASConfig.from_dict(data)` reads the `ssas` section and recognizes known fields only (unknown fields are ignored); `from_dict(None)` falls back to the defaults. `AgentSSASConfig.from_yaml(path)` reads the whole config.yaml and extracts the `ssas` section; a file without an `ssas` section is equivalent to the default configuration.
3. **`SSAS_`-prefixed environment variables**: applied on top of the above, with the highest precedence.

That is: `SSAS_` environment variables > the config.yaml `ssas` section > field defaults.

Environment variable parsing rules:

- Boolean fields: values are case-insensitive; `true` is truthy, everything else is falsy.
- Integer/float fields: if parsing fails, the environment variable is ignored (the original value is kept).
- `SSAS_MODE`: converted to the `AgentSSASMode` enum (`inprocess` / `http`); invalid values are ignored.
- `SSAS_HOME`: maps to the `ssas_home` field and also participates in storage root resolution (see below).

## Full AgentSSASConfig Field Reference

| Field | Type | Default | Environment variable | Description |
|-------|------|---------|----------------------|-------------|
| `enabled` | bool | `true` | `SSAS_ENABLED` | Global switch |
| `mode` | str (`inprocess` / `http`) | `inprocess` | `SSAS_MODE` | Operating mode |
| `http_endpoint` | str | `http://localhost:8443` | `SSAS_HTTP_ENDPOINT` | HTTP mode server address (used by the client) |
| `http_timeout` | float | `5.0` | `SSAS_HTTP_TIMEOUT` | HTTP client timeout in seconds |
| `http_token` | str / None | `None` | `SSAS_HTTP_TOKEN` | Bearer token authentication; enabled when not None |
| `http_host` | str | `0.0.0.0` | `SSAS_HTTP_HOST` | HTTP server listen address |
| `http_port` | int | `8443` | `SSAS_HTTP_PORT` | HTTP server port |
| `ssas_home` | str / None | `None` | `SSAS_HOME` | Storage root; when None, resolved via the environment variable chain |
| `storage_backend` | str | `sqlite` | — | Storage backend (Reserved, not effective in current version) |
| `event_ttl_days` | int | `30` | `SSAS_EVENT_TTL_DAYS` | Event retention in days (cleanup semantics below) |
| `alert_ttl_days` | int | `90` | `SSAS_ALERT_TTL_DAYS` | Alert retention in days (cleanup semantics below) |
| `rail_priority` | int | `80` | `SSAS_RAIL_PRIORITY` | Rail execution priority (Reserved, not effective in current version) |
| `enable_exception_hooks` | bool | `true` | `SSAS_ENABLE_EXCEPTION_HOOKS` | Exception hooks switch (Reserved, not effective in current version) |
| `risk_report_threshold` | str | `low` | `SSAS_RISK_REPORT_THRESHOLD` | Risk reporting threshold (safe/low/medium/high/critical) |
| `auth_timeout` | float | `2.0` | `SSAS_AUTH_TIMEOUT` | Timeout in seconds for auth-mode subscribers |
| `decision_policy` | str | `observe_only` | `SSAS_DECISION_POLICY` | Decision policy name |
| `modules` | dict | `{}` | — | Per detection module configuration |

Notes:

- `mode` corresponds to the `AgentSSASMode` enum: `inprocess` (in-process library mode, zero network latency) and `http` (standalone HTTP service mode, shared by multiple agents).
- `risk_report_threshold` takes one of the five risk levels (`safe` / `low` / `medium` / `high` / `critical`); risks below the threshold are not reported.
- `auth_timeout`: when an auth-mode subscriber does not return within this time, the module default policy applies.
- `modules` has no corresponding environment variable and can only be configured via config.yaml.
- `storage_backend`, `rail_priority`, `enable_exception_hooks`, and `risk_report_threshold` are reserved fields, not effective in the current version.

## TTL Data Cleanup

`event_ttl_days` and `alert_ttl_days` control the automatic cleanup of expired data. The cleanup mapping is:

- **Core database ssas_core.db**: the `events` and `raw_events` tables are cleaned per `event_ttl_days`; the `alerts` table is cleaned per `alert_ttl_days`;
- **Module databases**: each module's `process.db` is cleaned per `event_ttl_days`; `result.db` is cleaned per `alert_ttl_days`.

There are two trigger points:

1. once at startup, in `AgentSSASBackend.initialize()` (a cleanup failure does not affect initialization);
2. during operation, once every cumulative 100 event reports, triggered asynchronously in the background (never blocking the reporting path).

Setting `event_ttl_days` / `alert_ttl_days` to `<= 0` disables the cleanup of the corresponding tables (data is retained forever).

## Complete config.yaml `ssas` Section Example

```yaml
ssas:
  # Global switch
  enabled: true
  # Operating mode: inprocess (default) / http
  mode: inprocess

  # Storage configuration
  ssas_home:                  # When empty, resolved via SSAS_HOME > JIUWENSWARM_DATA_DIR > JIUWENSWARM_HOME > ~/.jiuwenswarm
  storage_backend: sqlite
  event_ttl_days: 30
  alert_ttl_days: 90

  # Rail configuration
  rail_priority: 80
  enable_exception_hooks: true
  risk_report_threshold: low

  # Decision policy: observe_only (default) / active_protection / custom policy name
  decision_policy: observe_only

  # Timeout in seconds for auth-mode subscribers
  auth_timeout: 2.0

  # HTTP mode only (effective when mode=http)
  http_endpoint: "http://localhost:8443"
  http_timeout: 5.0
  http_token:                  # Empty (Null) disables authentication; setting a value enables Bearer token authentication
  http_host: "0.0.0.0"
  http_port: 8443

  # Per detection module configuration
  modules:
    test_detection:
      enabled: true
```

Note: the JiuwenSwarm user config.yaml does not include an `ssas` section by default; you must add it explicitly and set `enabled: true` to enable SSAS.

## Storage Path Resolution

The storage root (`ssas_home`) is resolved with the following precedence:

```text
SSAS_HOME > JIUWENSWARM_DATA_DIR > JIUWENSWARM_HOME > ~/.jiuwenswarm
```

- If the `AgentSSASConfig.ssas_home` field (settable via config.yaml or the `SSAS_HOME` environment variable) is non-empty, it is used directly.
- If the field is None, the first non-empty value from the environment variable chain above is used.
- If none is set, it falls back to `~/.jiuwenswarm` under the user's home directory.

The final storage path rule is `storage_path = <ssas_home>/ssas`, that is:

```text
<ssas_home>/
└── ssas/                        # storage_path
    ├── ssas_core.db             # Core database (raw_events and other tables)
    ├── modules/<module_name>/   # Independent storage per detection module
    │   ├── process.db
    │   ├── result.db
    │   └── config/
    └── reports/threat_log/      # OCSF threat logs
```

## modules Section Structure

`modules` is a dictionary keyed by detection module name that provides per-module configuration. The current version supports overriding a module's enable switch:

```yaml
ssas:
  modules:
    agent_moss:
      enabled: true        # Overrides the enabled default from module.yaml
    security_rail_detection:
      enabled: true
    test_detection:
      enabled: false
```

Rules:

- Keys must exactly match the `name` field of the detection module's `module.yaml`; a typo does not raise an error, but the startup log emits a warning (`config.yaml 中配置的模块名 '<name>' 未找到对应的检测模块`).
- The value for each module is a dictionary; the `enabled` field is currently recognized. Modules with `enabled: false` are skipped during the startup scan (no plugin loading, no storage creation, no subscription registration).
- This section is a reserved extension point for runtime module configuration; future versions may carry more module-level parameters.

The module's own declaration (subscribed events, plugins, analysis parameters, etc.) is defined in module.yaml; see the [Detection Modules Reference](./detection-modules.md). For the steps to enable and disable modules, see [Enable and Disable Detection Modules](../how-to/enable-disable-detection-modules.md).

## Environment Variable Quick Reference

```bash
SSAS_ENABLED=true
SSAS_MODE=inprocess
SSAS_HTTP_ENDPOINT=http://localhost:8443
SSAS_HTTP_TIMEOUT=5.0
SSAS_HTTP_TOKEN=<token>
SSAS_HTTP_HOST=0.0.0.0
SSAS_HTTP_PORT=8443
SSAS_HOME=/path/to/home
SSAS_EVENT_TTL_DAYS=30
SSAS_ALERT_TTL_DAYS=90
SSAS_RAIL_PRIORITY=80
SSAS_ENABLE_EXCEPTION_HOOKS=true
SSAS_RISK_REPORT_THRESHOLD=low
SSAS_AUTH_TIMEOUT=2.0
SSAS_DECISION_POLICY=observe_only
```

In addition, three environment variables participate only in the storage root resolution chain: `JIUWENSWARM_DATA_DIR` and `JIUWENSWARM_HOME` (neither carries the `SSAS_` prefix), plus `SSAS_HOME` at the head of the chain, which also maps to the `ssas_home` field.

## Related Documentation

- [Event Format Reference](./event-format.md)
- [Detection Modules Reference](./detection-modules.md)
- [HTTP API Reference](./http-api.md)
- Repository root [README.md](../../../README.md)
