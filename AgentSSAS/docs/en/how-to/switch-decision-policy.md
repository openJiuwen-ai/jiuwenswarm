# Switch Decision Policy

The decision policy controls how AgentSSASSecurityRail maps the risk level of the `RiskAssessment` returned by AgentSSASCore to a `SecurityDecision` (allow, alert, or reject). This guide explains the differences between the two built-in policies and how to switch between them.

Prerequisites:

- [Configuration Reference](../reference/configuration.md): the `decision_policy` field and its environment variable
- [Detection Modules Reference](../reference/detection-modules.md): notify and auth subscription modes

## Built-in Policies Compared

Policies are defined in `src/agent_ssas/backend_client/openjiuwen/decision_policies.yaml`; the risk level to action mapping is:

| Risk level | observe_only (default) | active_protection |
|------------|------------------------|-------------------|
| critical | allow | reject |
| high | allow | alert |
| medium | allow | alert |
| low | allow | alert |
| safe | allow | allow |

- `observe_only`: observation only, everything is allowed. The system performs situational awareness only and exerts no decision impact on the JiuwenSwarm runtime. This is the default policy; it avoids duplicate blocking in combination with other security rails (such as PermissionInterruptRail).
- `active_protection`: acts by risk level. critical is rejected, high/medium/low raise alerts, and safe is allowed.

The mapping from policy actions to agent-core `SecurityDecision` values is performed by `EventReporter.assessment_to_decision`:

| Policy action | SecurityDecision | Description |
|---------------|------------------|-------------|
| `reject` | `SecurityReject` | Blocks the current operation and carries the full `risk_assessment` |
| `alert` | `SecurityAlert` | Raises an alert; the alert level is determined by the `alert_levels` section |
| `allow` | `SecurityAllow` | Allows the operation |

Alert level mapping for the `alert` action (the `alert_levels` section of `decision_policies.yaml`):

| Risk level | AlertLevel |
|------------|------------|
| critical | error |
| high | error |
| medium | warning |
| low | info |

## Switching Methods

### Option 1: Edit config.yaml

Set `decision_policy` in the `ssas` section of JiuwenSwarm's `config.yaml`:

```yaml
ssas:
  enabled: true
  mode: inprocess
  decision_policy: active_protection
```

### Option 2: Override via Environment Variable

The environment variable `SSAS_DECISION_POLICY` takes precedence over config.yaml, which makes it suitable for temporary switching or containerized deployments:

```bash
export SSAS_DECISION_POLICY=active_protection
```

Windows PowerShell:

```powershell
$env:SSAS_DECISION_POLICY = "active_protection"
```

Restart JiuwenSwarm (or the SSAS HTTP service) after the change for it to take effect.

If an unknown policy name is configured, the loader logs a warning and falls back to the default policy `observe_only`; startup does not fail.

## Custom Policies

Policies are defined centrally in `decision_policies.yaml` (in the same directory as `policy_loader.py`, shipped with the package):

```yaml
policies:
  observe_only:
    critical: allow
    high: allow
    medium: allow
    low: allow
    safe: allow

  active_protection:
    critical: reject
    high: alert
    medium: alert
    low: alert
    safe: allow

alert_levels:
  critical: error
  high: error
  medium: warning
  low: info

default_policy: observe_only
```

To define a custom policy:

1. Add a new policy name under the `policies` section (for example, `strict_protection`) and assign an action (`reject` / `alert` / `allow`) to each of the five risk levels (critical/high/medium/low/safe).
2. Reference the new policy name via `ssas.decision_policy` in config.yaml (or `SSAS_DECISION_POLICY`).
3. If you want to change the default fallback behavior for the new policy, modify `default_policy` accordingly; both an unconfigured policy name and an unknown policy name fall back to `default_policy`.

The policy file is loaded into the process and cached; it is not re-read at runtime, so a restart is required after changes.

## Note: In `notify` Mode, `reject` Is a Recommendation, Not an Enforced Block

The 3 built-in detection modules (`agent_moss`, `security_rail_detection`, `test_detection`) all subscribe to events in `notify` mode: detection runs asynchronously in the background, and `report_event` does not wait for the detection result to return. Therefore, under notify mode:

- The `reject` action of `active_protection` does not actually block the current JiuwenSwarm operation. Instead, it is recorded in the detection result as a recommended decision (in `RiskAssessment.recommended_actions`, the module's result.db, and fields such as `evidences[].data.decision` in the threat log), for after-the-fact auditing and alert consumption.
- For `reject` to actually participate synchronously in runtime decisions, the detection module must subscribe to events in `auth` mode (append the `:auth` suffix to the subscription string in `module.yaml`, for example `"tool_input:auth"`), combined with configuration such as `auth_timeout`. See the [Detection Modules Reference](../reference/detection-modules.md).

## Related Documentation

- [Enable and Disable Detection Modules](./enable-disable-detection-modules.md)
- [View Threat Logs](./view-threat-logs.md)
- [Configuration Reference](../reference/configuration.md)
