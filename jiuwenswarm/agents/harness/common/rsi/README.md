# Harness RSI on dev-stable

This service runs single-Harness experiments with the paired agent-core RSI
engine. It does not register AutoHarness or Paper/Program optimization engines.

Enable the experiment entry through the workspace configuration, then restart
the service or reconnect the Web client:

```yaml
rsi:
  enabled: true
```

The default is disabled. `rsi.capabilities` returns the effective switch;
disabled requests cannot start experiments. Web creation exposes Harness only.
Runtime pause is unavailable, queued tasks can pause, and running tasks stop
through cancellation. The service recovers task metadata and engine snapshots
after restart without silently restarting paid model work.

Orderly AgentServer shutdown cancels and awaits the worker's owned tasks.
Running work follows the existing cancellation semantics and becomes
`TERMINATED`; queued work is retained and becomes `PAUSED` during restart
recovery. A closed worker rejects further enqueue/resume requests.

Inputs are copied into task-local datasets, model configurations and Harness
versions. The engine's published package is validated through
`openjiuwen.rsi.harness_rsi.package_compat`, then installed into the RSI version
store. Existing cached agents switch through the stable ExpertHarness load and
unload APIs; newly created agents restore the active version. Loading errors
must preserve or restore the previous version.

RSI versions are independent of the newer Plugin catalog. The production
factory registers only the HARNESS adapter. Unsupported artifact scenarios and
package declarations fail explicitly.

New tasks record continuous scoring and the two-stage promotion policy in the
task profile. Resume uses the policy in the existing task snapshot. Harness
previews use the task's immutable snapshot ID and leave the active artifact
reference intact.

`uv sync --locked` uses the matching rebased agent-core commit pinned in
`pyproject.toml` and `uv.lock`.
