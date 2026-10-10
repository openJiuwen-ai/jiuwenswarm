# Enable and Disable Detection Modules

AgentSSASCore scans the detection module directory at startup and loads all enabled modules. This guide explains how to enable or disable detection modules either by overriding configuration or by editing `module.yaml` directly, and how to verify the result after the system runs.

Prerequisites:

- [Detection Modules Reference](../reference/detection-modules.md): complete field reference for module declaration files
- [Configuration Reference](../reference/configuration.md): structure of the `ssas.modules` section

## Default State of Built-in Modules

| Module name | Default state | Subscribed events |
|-------------|---------------|-------------------|
| `agent_moss` | enabled | 6 lifecycle events |
| `security_rail_detection` | enabled | `permission_interrupt_tool` |
| `test_detection` | disabled | `*` (all events) |

## Option 1: Override via config.yaml (Recommended)

In the `ssas` section of JiuwenSwarm's `config.yaml`, override the default value from `module.yaml` via `modules.<module_name>.enabled`, without touching the source code:

```yaml
ssas:
  enabled: true
  mode: inprocess
  modules:
    # Enable the test module, which is disabled by default
    test_detection:
      enabled: true
    # Disable agent_moss, which is enabled by default
    agent_moss:
      enabled: false
```

Override rules:

- The override takes effect only when the module name matches the `name` field of a loaded module, and currently only the `enabled` field can be overridden.
- If a module name configured in `config.yaml` has no corresponding detection module (for example, due to a typo), a warning appears in the startup log: `config.yaml 中配置的模块名 '<name>' 未找到对应的检测模块，请检查拼写是否正确`.
- This approach does not modify any files inside the package and remains effective after upgrades or reinstalls, making it the recommended way to toggle modules at runtime.

## Option 2: Edit module.yaml Directly (Source Code Approach)

Each detection module's declaration file is located inside the package:

```text
src/agent_ssas/core/detection_modules/<module_name>/module.yaml
```

Edit its `enabled` field directly. To enable `test_detection`, for example, modify `src/agent_ssas/core/detection_modules/test_detection/module.yaml`:

```yaml
name: test_detection
display_name: "测试检测模块"
enabled: true
event_version: "1.0"
subscribed_events: ["*"]
```

Notes:

- This approach modifies files inside the installed package. With an editable install (`uv pip install -e .`), edits to the source directory take effect immediately; with a regular (non-editable) install, you are editing the copy in site-packages, which is overwritten on reinstall or upgrade.
- It is suitable for developers debugging custom modules; for day-to-day operations, prefer Option 1.

## Restart to Apply Changes

Detection modules are scanned, loaded, and wired into the subscription table once, when `DetectionModuleManager` initializes (that is, when the SSAS backend starts); they are not re-scanned while the system is running. Therefore, regardless of which approach you take, JiuwenSwarm (inprocess mode) or the SSAS HTTP service (http mode) must be restarted for the change to take effect.

## Verification

### Check the Startup Log

Module loading results are written to the log:

- Loaded successfully: `检测模块加载完成: name=<module_name>, events=[...]`
- Skipped because not enabled: `检测模块未启用,跳过: <module_name>`

After enabling `test_detection`, the startup log should contain a load record with `name=test_detection`; after disabling `agent_moss`, you should see `检测模块未启用,跳过: agent_moss`.

### Check the Storage Directory

Each successfully loaded module creates its own subdirectory under the storage root (containing the process database, the result database, and a config directory):

```text
<storage>/modules/<module_name>/
├── process.db    # Process data (modeled data, intermediate results)
├── result.db     # Result data (alerts, audits, threat analysis reports)
└── config/       # Configuration data (rules, baselines, threshold parameters)
```

Here `<storage>` defaults to `~/.jiuwenswarm/ssas/` and can be adjusted via environment variables such as `SSAS_HOME`; see the [Configuration Reference](../reference/configuration.md).

Once a module is enabled, the first event processing triggers the creation of the directory and the database files. If the `modules/<module_name>/` directory has not been created, the module was not loaded (either not enabled or failed to load; on load failure the log records the exception stack trace).

## Related Documentation

- [Switch Decision Policy](./switch-decision-policy.md)
- [View Threat Logs](./view-threat-logs.md)
- [Detection Modules Reference](../reference/detection-modules.md)
