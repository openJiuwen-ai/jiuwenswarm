# 切换决策策略

决策策略（decision policy）控制 AgentSSASSecurityRail 在收到 AgentSSASCore 返回的 `RiskAssessment` 后，如何将风险等级映射为 `SecurityDecision`（放行、告警或阻断）。本文说明两种内置策略的差异与切换方法。

前置阅读：

- [配置参考](../reference/configuration.md)：`decision_policy` 字段与环境变量
- [检测模块参考](../reference/detection-modules.md)：notify 与 auth 订阅模式

## 内置策略对照

策略定义在 `src/agent_ssas/backend_client/openjiuwen/decision_policies.yaml` 中，风险等级到动作的映射如下：

| 风险等级 | observe_only（默认） | active_protection |
|---------|--------------------|-------------------|
| critical | allow（放行） | reject（阻断） |
| high | allow（放行） | alert（告警） |
| medium | allow（放行） | alert（告警） |
| low | allow（放行） | alert（告警） |
| safe | allow（放行） | allow（放行） |

- `observe_only`：仅观察，全部放行，系统只做态势感知，不对 JiuwenSwarm 运行时产生任何决策影响。这是默认策略，可避免与其他安全 Rail（如 PermissionInterruptRail）产生二次阻断。
- `active_protection`：按风险等级处理。critical 阻断，high/medium/low 产生告警，safe 放行。

动作到 agent-core `SecurityDecision` 的映射由 `EventReporter.assessment_to_decision` 执行：

| 策略动作 | SecurityDecision | 说明 |
|---------|-----------------|------|
| `reject` | `SecurityReject` | 阻断当前操作，携带完整 `risk_assessment` |
| `alert` | `SecurityAlert` | 产生告警，告警级别由 `alert_levels` 段决定 |
| `allow` | `SecurityAllow` | 放行 |

`alert` 动作的告警级别映射（`decision_policies.yaml` 的 `alert_levels` 段）：

| 风险等级 | AlertLevel |
|---------|-----------|
| critical | error |
| high | error |
| medium | warning |
| low | info |

## 切换方法

### 方式一：修改 config.yaml

在 JiuwenSwarm 的 `config.yaml` 的 `ssas` 段中设置 `decision_policy`：

```yaml
ssas:
  enabled: true
  mode: inprocess
  decision_policy: active_protection
```

### 方式二：环境变量覆盖

环境变量 `SSAS_DECISION_POLICY` 的优先级高于 config.yaml，适合临时切换或容器化部署：

```bash
export SSAS_DECISION_POLICY=active_protection
```

Windows PowerShell：

```powershell
$env:SSAS_DECISION_POLICY = "active_protection"
```

修改后重启 JiuwenSwarm（或 SSAS HTTP 服务）生效。

如果配置了未知的策略名，加载器会记录警告并回退到默认策略 `observe_only`，不会导致启动失败。

## 自定义策略

策略集中定义在 `decision_policies.yaml`（与 `policy_loader.py` 同目录，随包分发）：

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

自定义策略的思路：

1. 在 `policies` 段下新增一个策略名（例如 `strict_protection`），为五个风险等级（critical/high/medium/low/safe）各指定一个动作（`reject` / `alert` / `allow`）。
2. 通过 config.yaml 的 `ssas.decision_policy`（或 `SSAS_DECISION_POLICY`）引用新策略名。
3. 如需调整新策略的默认回退行为，可同步修改 `default_policy`；未配置策略名或策略名未知时都会回退到 `default_policy`。

策略文件在进程内加载后缓存，运行期不会重新读取，修改后需重启。

## 注意事项：notify 模式下 reject 是建议而非强制阻断

当前 3 个内置检测模块（`agent_moss`、`security_rail_detection`、`test_detection`）均以 `notify` 模式订阅事件：检测在后台异步执行，`report_event` 不等待检测结果返回。因此在 notify 模式下：

- `active_protection` 的 `reject` 动作不会真正阻断 JiuwenSwarm 的当前操作，而是作为建议决策记录在检测结果中（`RiskAssessment.recommended_actions`、模块 result.db 与威胁日志的 `evidences[].data.decision` 等字段），供事后审计与告警消费。
- 若要 `reject` 真正同步参与运行时决策，需要检测模块以 `auth` 模式订阅事件（`module.yaml` 订阅字符串加 `:auth` 后缀，如 `"tool_input:auth"`），并配合 `auth_timeout` 等配置。详见[检测模块参考](../reference/detection-modules.md)。

## 相关文档

- [启用与禁用检测模块](./enable-disable-detection-modules.md)
- [查看威胁日志](./view-threat-logs.md)
- [配置参考](../reference/configuration.md)
