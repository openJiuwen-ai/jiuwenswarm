# Runtime Worker shutdown, upgrades, and recovery

A resident Worker keeps Runtime loaded across Front restarts for desktop and ordinary services. The application or service owner controls final shutdown. HarmonyOS defaults to `front_loopback`: its Worker exits when the Front disconnects and does not use resident endpoint election or reattachment. Both modes verify actual process exit at final shutdown.

## Startup and configuration readiness

Front prepares control-plane configuration reads first. The Web application dismisses its loading-config notification after `config.get` succeeds for the current connection and the configuration is applied. Model discovery and execution resources can continue preparing; configuration readiness does not imply Agent execution readiness. Reconnection refreshes the configuration, and a failed read exposes an error and retry action.

After connection, users can open Add model and fill a draft while the existing model list and vendor catalog load. Saving still waits for both to preserve validation, identity, and conflict checks. Background results retain entered fields; initialization failures allow retrying or canceling the draft.

Worker IPC uses v3. The Supervisor validates the HELLO protocol, process birth identity, build identity, and data directory before granting startup permission; old-protocol Workers are not reused directly. Protocol v2 and newer require a verifiable creation timestamp or process start counter. Safe SDK imports can start earlier, while configuration migration, Runtime resource construction, and execution remain gated by startup permission.

Bounded `runtime_prewarm` is enabled by default for one configured profile. It creates no Session and sends no model request. Defaults are a 1-second idle delay, two attempts, and budgets of 10 seconds wall time, 5 seconds process CPU, and 256 MB RSS growth. Foreground activity prevents subsequent background steps. Synchronous imports cannot be forcibly preempted; CPU/RSS budgets are observations that stop subsequent work. Set `runtime_prewarm.enabled: false` to disable prewarm. Measure true cold starts with prewarm disabled and report them separately from product-default results.

Enabling or disabling a Rail extension updates the SDK instances registered by the current Worker's session adapters across channels and projects. Each instance owns its Rail object, and the RPC acknowledges success after those updates complete. With no instance yet, only configuration is saved; future instances load the current setting without creating an Agent or Session for a configuration request. An instance update failure returns an error and can be retried; completed registrations remain idempotent.

## Shutdown, upgrades, and recovery

- In resident mode, a normal Front restart or Supervisor `stop()` detaches and preserves the Worker.
- In HarmonyOS's default `front_loopback` mode, Supervisor `stop()` shuts down and reaps its attached Worker.
- Final desktop exit and upgrade stop the Front, then explicitly drain the current deployment's Worker, including a reattached Worker.
- Closing a macOS window retains the existing hide/background behavior. Quit performs final cleanup.
- Quit during workspace preparation cancels subsequent service startup. Full desktop mode also checks for a resident Worker before AgentServer has started; frontend-only mode leaves its external backend running.
- OS termination, power loss, and interpreter crashes cannot guarantee cleanup. The next launch validates identity and handles old endpoints.

Endpoints are scoped by installation, Python environment, data directory, and runtime profile. HELLO validates code/build identity, installed dependencies, and process creation time. Changed source, dependencies, or native build instances cannot silently reuse old code. Each wheel or native backend build receives a fresh ID, even with identical inputs. Wheels validate contents at build time; startup reads the artifact manifest and observes installed dependencies. Source checkouts continue content validation.

When a HarmonyOS sandbox denies access to global `/proc/stat`, the Worker and Supervisor can validate process identity using the start counter in `/proc/<pid>/stat`, transmitted separately from the creation timestamp. If neither source is readable, startup fails with a HELLO log containing the PID, stage, and traceback. Cleanup also refuses to terminate an unverified target based on its PID alone.

Successful shutdown requires a successful acknowledgement and observed PID exit. Forced termination, missing acknowledgements, and negative acknowledgements report failure. Install helpers also require this update attempt's successful shutdown receipt before replacing backend files. Legacy peers without acknowledgement support use verified process exit as their compatibility condition.

On macOS, both native desktop builds and wheel installations may observe clock drift when retiring an older Worker that reports only a wall-clock creation time. Shutdown may proceed over the existing Unix socket if the kernel reports the same peer PID and the usual deployment, user, executable, and socket checks pass.

Instances sharing a data directory retain distinct endpoint and deployment identities through `JIUWENSWARM_RUNTIME_INSTANCE` (preferred) or `HOSTNAME`. Stop commands must retain that instance's environment. Historical Workers owned by another instance are left running.

The final owner command also checks historical endpoints before AgentServer has run startup migration. It retires only Workers with verifiable ownership; an unversioned Python Worker still requires explicit PID selection.

Historical endpoint discovery includes the POSIX temporary-directory fallback used by long workspace paths. Migration shares the current startup election deadline. If failed-Worker retirement is refused or its exit cannot be observed, recovery returns failure, retains the FAILED state and owned process handle for retry, and does not start a replacement.

Desktop process-tree cleanup continues for proven service branches when another branch cannot be inspected; it preserves Worker and unproven branches and reports incomplete cleanup. On macOS, an update helper exiting after READY must leave the shutdown receipt available to the application writer. The helper removes its attempt files only after observing the application's exit.

`jiuwenswarm-stop` manages the state created by `jiuwenswarm-start debug`. Failed cleanup preserves that state for retry; no debug state remains a no-op.

Debug shutdown defaults to separate budgets: 15 seconds for the Front and 35 seconds for the Worker. The Worker budget covers its internal 30-second cleanup deadline plus IPC and process-exit observation. These phases run sequentially; their budgets do not impose a single total command timeout.
An explicit `--timeout N` still assigns N seconds to each phase. `--worker-timeout N` overrides only the Worker phase and can be combined with `--timeout`. Timeout values must be positive and finite.
`make stop`, `make restart-debug`, and `make restart-debug-fast` forward `STOP_ARGS` to the stop command. A failed stop prevents the restart. For example, adjust only the Worker budget:

```bash
make restart-debug-fast STOP_ARGS="--worker-timeout 45"
```

Wheel launchers `jiuwenswarm-start all/app/dev`, named instances, and standalone `jiuwenswarm-app` stop Fronts and then retire their Worker on final exit. `--stop <name>` and a full instance `--restart <name>` also complete final shutdown. Named-instance shutdown loads the selected workspace's bootstrap and runtime environment in a separate lifecycle process. It holds the startup lock through Worker exit confirmation, deleting the PID record only after success and retaining it on failure. Windows service-tree cleanup preserves Worker branches for the final IPC operation. A web-only launcher leaves its external backend running.

Python desktop final exit checks existing Workers and verifiable historical endpoints even before registering its first service. When a Worker process is still booting before IPC exists, final shutdown waits for HELLO within the same deadline and then validates ownership. Command-line observations authorize waiting, never retirement.
When a background thread owns cleanup, the desktop entry point waits for it to finish. The wait ends at the 30-second shutdown deadline plus five seconds for final process cleanup and writing the update receipt. If the wait expires, the desktop logs incomplete shutdown. The installer still requires a successful receipt.

A service manager controlling `jiuwenswarm-agentserver` directly must still distinguish Front restart from application final stop. For final stop, stop its Front first, then use the same installation environment:

```bash
python -m jiuwenswarm.server.supervisor.shutdown --shutdown-worker --timeout 30
```

On Windows, Ctrl+C can also reach a Worker sharing the console. The Worker converts the signal into
one task cancellation and waits for Runtime, background tasks, and in-flight IPC I/O cleanup before
exiting. Repeated Ctrl+C does not interrupt cleanup already in progress. The launcher may return 130
for user interruption; complete Worker shutdown still requires no cleanup errors and observed process
exit. Ordinary Front disconnection continues to preserve a resident Worker, and OS termination cannot
guarantee cleanup.

If first-time Runtime preparation exceeds the default 30-second shutdown budget, the Worker reports incomplete shutdown and exits nonzero. An absent process alone does not establish successful graceful cleanup.

An old Python Worker without deployment identity cannot be attributed to an installation solely from its shared interpreter. Startup isolates that old endpoint and continues, logging the old PID. After checking its interpreter, command line, installation, and data directory, an operator can select it explicitly:

```bash
jiuwenswarm-stop --legacy-worker-pid 12345
# After verifying the old Worker's installation, select it during restart:
make restart-debug STOP_ARGS="--legacy-worker-pid 12345"
```

Selection still verifies the user, interpreter, Worker module, IPC argument, and process creation time; a declared different deployment is refused. Timeout or changed process identity reports failure. Stop its Front first so that another supervisor cannot restart it.

Exhausted startup/IPC retry budgets publish `FAILED`, then retry after 30 seconds. Consecutive failures back off through 60, 120, 240, and at most 300 seconds. Success ends that cooldown cycle; stopping the service cancels background recovery. Permanent configuration, protocol, identity, and Runtime-load errors require correction and explicit reload. Automatic recovery does not replay failed requests or establish that OS memory pressure has been resolved.

On a transient resident IPC disconnect, Front retains submitted streaming waiters and their delivered-event cursors for up to 90 seconds of transport recovery and replay acknowledgement. Reattachment must validate the same Worker instance, PID, and process creation time. Output resumes for the original dispatch and caller scope; task input and tool operations are never resubmitted. The Worker retains stream completion during detachment and enables live delivery only after replay. Front filters replay duplicates using the delivered cursor.

The 90-second budget applies to transport recovery, not normal execution time. Confirmed Worker exit, replacement or unhealthy retirement, exhausted reconnect attempts, an unavailable dispatch, expired undelivered replay, and service stop still fail the original waiter explicitly. Missing output cannot imply success. Non-streaming RPCs retain their delivery acknowledgement and failure semantics; `front_loopback` retains its existing Worker lifetime behavior.

If both IPC send and close fail, Front retains the original recovery waiters. A failed recovery subscription retires that connection before all preserved streams resume. Service stop cleans up waiters and background tasks even when IPC close raises.

## Process identity

IPC protocol 2 HELLO messages must include a valid creation timestamp or process start counter. Missing or null values for both fields reject the handshake. Protocol 1 retains compatibility with missing birth fields and still requires OS ownership checks. Timestamps and start counters retain separate units; booleans and nonpositive values are invalid.

Debug service state and new DingTalk OAuth PID records retain an OS process identity, including start counters on restricted systems. An unreadable identity remains unknown: debug stop refuses the signal, and OAuth cancellation reports incomplete cleanup while retaining pending state and credentials. Legacy timestamp records remain readable where their identity can be verified.

When debug service identity cannot be verified, an explicit `--legacy-worker-pid` can still enter independent Worker ownership validation and cleanup. No signal is sent to the unverified Front, debug state is retained, and a nonzero exit code reports that overall service shutdown remains unconfirmed. If OAuth timeout cleanup fails during identity revalidation or exit waiting, status queries return `authorization_cleanup_failed`, retain pending records and credentials, and do not write an `expired` exit marker. Later queries continue to check the process state.

develop also sends the optional native `process_started` marker to tolerate clock-correction differences between observers. If that API is unavailable, the marker is null and valid ticks remain usable. It cannot replace the creation time or ticks required by protocol 2, or relax legacy timestamp identity checks. Comparable ticks still take priority.

## Worker output logs

For both resident and `front_loopback` Workers, stdout inherits the launching process's output, while stderr is appended directly to a separate `runtime-worker-<UUID>[-<owner>].stderr.log` for each launch. Set `JIUWEN_WORKER_LOG_DIR` before starting the application or Supervisor to select its directory. If unset or empty, logs use `worker-logs` under the parent of the current Worker's socket path. A custom socket path therefore changes the default log directory; `front_loopback` also uses the supplied socket path to select this default. This setting does not change the IPC endpoint.

Use an absolute path for `JIUWEN_WORKER_LOG_DIR` where possible. Relative paths are resolved against the Supervisor's current working directory.

The Supervisor does not read and relabel individual Worker output lines or create a separate stdout log file. Stderr can contain ordinary INFO records. Diagnose failures using the original log levels, exception tracebacks, and process exit code.

The same directory contains `latest-worker-error.log`, recording the most recent Worker's error diagnostics with its PID, exception type, message, and traceback. Before spawning a new Worker, the Supervisor attempts to remove the previous diagnostic and passes its path through the internal `JIUWEN_WORKER_DIAGNOSTIC_PATH` environment variable, which normally needs no manual configuration. This file is not a permanent per-Worker archive.

Workers using the same log directory share this fixed diagnostic file: starting a new Worker may clear another Worker's diagnostics, and concurrent writes may interleave. Use a separate log directory for each instance when diagnostic isolation is needed. Each launch's separate stderr file remains available for troubleshooting.

The Worker records top-level exceptions, HELLO failures, and ERROR logs, including unhandled asyncio task exceptions. HELLO failure diagnostics are persisted before closing IPC so that the parent can read them when cleanup starts; ordinary ERROR logs retain background writing. Diagnostic write failures do not replace the original exception. When Worker startup fails, the Supervisor reports the diagnostic path and at most the final 16 KiB while reaping the child. Missing or unreadable diagnostics report their unavailability. Ordinary stderr remains in its original file.

Worker stderr reuses the application logging limits: rotate at 20 MiB and retain at most 20 archives. A Worker-owned background thread checks every five seconds, copies the output into archives, and truncates the original file without replacing its descriptor. A resident Worker continues rotation after its Front exits. Archives are named `runtime-worker-<UUID>[-<owner>].stderr_<UTC-date>_<UTC-time>_<nanoseconds>.log`, with a strictly increasing per-Worker suffix and each at most 20 MiB. Chunk boundaries preserve complete UTF-8 characters. Bursts spanning multiple chunks produce multiple archives. Older output is dropped beyond the full archive budget, retaining at most the most recent 400 MiB. The active file can temporarily exceed the threshold between checks.

The Worker maintenance thread cleans up historical files on its first run and every 60 seconds. Front startup does not wait for a directory scan:

- Worker outputs confirmed closed and their completed archives share a limit of the newest 20 eligible files and 400 MiB.
- `supervisor-<PID>-<timestamp>.txt` snapshots separately retain the newest 20 files and at most 400 MiB.
- Temporary archives left by interrupted rotation are removed once confirmed closed and past the 60-second protection period, preserving their complete source log.
- New Linux/macOS base filenames include an ownership token. Linux derives it from the kernel boot UUID, PID/user namespaces, namespace init start time, and effective UID; macOS uses the kernel boot-session UUID and effective UID. POSIX cleanup requires a matching local token and a complete local handle scan before deleting a base log or temporary archive. It retains foreign tokens, legacy files without tokens, and files whose ownership cannot be established, including logs from previous boots or namespaces; these files remain outside the cleanup budget. A local scan or an old modification time cannot prove that a remote Pod has closed its file. Completed archives remain eligible regardless of their token.
- Files modified within the last 60 seconds are temporarily protected. Windows probes file delete sharing to protect inherited stderr handles without enumerating system processes. POSIX matches numeric real, effective, and saved UIDs before inspecting open files, so unresolved usernames and inaccessible files in other users' processes do not block cleanup. If process ownership or handles remain inaccessible, or the scan exceeds two seconds, base logs are retained with a warning; a partial scan is never treated as proof of closure. Completed archives can still be pruned. Archives that cannot be deleted while open in a viewer are retried later without blocking active-file rotation.

Copy/truncate follows the application logger's existing policy; writes concurrent with the interval between the copied snapshot and truncation can be lost. Maintenance runs outside the business event loop, failures produce warnings, and shutdown waits at most 0.2 seconds for the maintenance thread. When a configured stderr file cannot be validated at startup, rotation stays disabled and emits a warning; missing parent configuration remains an expected silent skip. Limits reuse `_LOG_FILE_MAX_BYTES` and `_LOG_FILE_BACKUP_COUNT` in `common/utils.py`; no `config.yaml` option is added.
