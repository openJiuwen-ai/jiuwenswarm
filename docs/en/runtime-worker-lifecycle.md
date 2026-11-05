# Runtime Worker shutdown, upgrades, and recovery

A resident Worker keeps Runtime loaded across Front restarts for desktop and ordinary services. The application or service owner controls final shutdown. HarmonyOS defaults to `front_loopback`: its Worker exits when the Front disconnects and does not use resident endpoint election or reattachment. Both modes verify actual process exit at final shutdown.

- In resident mode, a normal Front restart or Supervisor `stop()` detaches and preserves the Worker.
- In HarmonyOS's default `front_loopback` mode, Supervisor `stop()` shuts down and reaps its attached Worker.
- Final desktop exit and upgrade stop the Front, then explicitly drain the current deployment's Worker, including a reattached Worker.
- Closing a macOS window retains the existing hide/background behavior. Quit performs final cleanup.
- Quit during workspace preparation cancels subsequent service startup. Full desktop mode also checks for a resident Worker before AgentServer has started; frontend-only mode leaves its external backend running.
- OS termination, power loss, and interpreter crashes cannot guarantee cleanup. The next launch validates identity and handles old endpoints.

Endpoints are scoped by installation, Python environment, data directory, and runtime profile. HELLO validates code/build identity, installed dependencies, and process creation time. Changed source, dependencies, or native build instances cannot silently reuse old code. Each wheel or native backend build receives a fresh ID, even with identical inputs. Wheels validate contents at build time; startup reads the artifact manifest and observes installed dependencies. Source checkouts continue content validation.

Successful shutdown requires a successful acknowledgement and observed PID exit. Forced termination, missing acknowledgements, and negative acknowledgements report failure. Install helpers also require this update attempt's successful shutdown receipt before replacing backend files. Legacy peers without acknowledgement support use verified process exit as their compatibility condition.

Instances sharing a data directory retain distinct endpoint and deployment identities through `JIUWENSWARM_RUNTIME_INSTANCE` (preferred) or `HOSTNAME`. Stop commands must retain that instance's environment. Historical Workers owned by another instance are left running.

The final owner command also checks historical endpoints before AgentServer has run startup migration. It retires only Workers with verifiable ownership; an unversioned Python Worker still requires explicit PID selection.

Historical endpoint discovery includes the POSIX temporary-directory fallback used by long workspace paths. Migration shares the current startup election deadline. If failed-Worker retirement is refused or its exit cannot be observed, recovery returns failure, retains the FAILED state and owned process handle for retry, and does not start a replacement.

Desktop process-tree cleanup continues for proven service branches when another branch cannot be inspected; it preserves Worker and unproven branches and reports incomplete cleanup. On macOS, an update helper exiting after READY must leave the shutdown receipt available to the application writer. The helper removes its attempt files only after observing the application's exit.

`jiuwenswarm-stop` manages the state created by `jiuwenswarm-start debug`. Failed cleanup preserves that state for retry; no debug state remains a no-op.

Wheel launchers `jiuwenswarm-start all/app/dev`, named instances, and standalone `jiuwenswarm-app` stop Fronts and then retire their Worker on final exit. `--stop <name>` and a full instance `--restart <name>` also complete final shutdown. Named-instance shutdown loads the selected workspace's bootstrap and runtime environment in a separate lifecycle process. It holds the startup lock through Worker exit confirmation, deleting the PID record only after success and retaining it on failure. Windows service-tree cleanup preserves Worker branches for the final IPC operation. A web-only launcher leaves its external backend running.

Python desktop final exit checks existing Workers and verifiable historical endpoints even before registering its first service. When a Worker process is still booting before IPC exists, final shutdown waits for HELLO within the same deadline and then validates ownership. Command-line observations authorize waiting, never retirement.

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
jiuwenswarm-stop --legacy-worker-pid 12345 --timeout 30
```

Selection still verifies the user, interpreter, Worker module, IPC argument, and process creation time; a declared different deployment is refused. Timeout or changed process identity reports failure. Stop its Front first so that another supervisor cannot restart it.

Exhausted startup/IPC retry budgets publish `FAILED`, then retry after 30 seconds. Consecutive failures back off through 60, 120, 240, and at most 300 seconds. Success ends that cooldown cycle; stopping the service cancels background recovery. Permanent configuration, protocol, identity, and Runtime-load errors require correction and explicit reload. Automatic recovery does not replay failed requests or establish that OS memory pressure has been resolved.
