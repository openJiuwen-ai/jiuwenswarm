# Runtime Worker shutdown, upgrades, and recovery

A resident Worker keeps Runtime loaded across Front restarts. The application or service owner controls final shutdown.

- A normal Front restart or Supervisor `stop()` detaches and preserves the Worker.
- Final desktop exit and upgrade stop the Front, then explicitly drain the current deployment's Worker, including a reattached Worker.
- Closing a macOS window retains the existing hide/background behavior. Quit performs final cleanup.
- OS termination, power loss, and interpreter crashes cannot guarantee cleanup. The next launch validates identity and handles old endpoints.

Endpoints are scoped by installation, Python environment, data directory, and runtime profile. HELLO validates code/build identity, installed dependencies, and process creation time. Changed source, dependencies, or native build instances cannot silently reuse old code. Each wheel or native backend build receives a fresh ID, even with identical inputs. Wheels validate contents at build time; startup reads the artifact manifest and observes installed dependencies. Source checkouts continue content validation.

Successful shutdown requires a successful acknowledgement and observed PID exit. Forced termination, missing acknowledgements, and negative acknowledgements report failure. Install helpers also require this update attempt's successful shutdown receipt before replacing backend files. Legacy peers without acknowledgement support use verified process exit as their compatibility condition.

`jiuwenswarm-stop` manages the state created by `jiuwenswarm-start debug`. Failed cleanup preserves that state for retry; no debug state remains a no-op. For a regular wheel service, stop its Front through its terminal/service manager, then use the same installation environment:

```bash
python -m jiuwenswarm.server.supervisor.shutdown --shutdown-worker --timeout 30
```

An old Python Worker without deployment identity cannot be attributed to an installation solely from its shared interpreter. Startup isolates that old endpoint and continues, logging the old PID. After checking its interpreter, command line, installation, and data directory, an operator can select it explicitly:

```bash
jiuwenswarm-stop --legacy-worker-pid 12345 --timeout 30
```

Selection still verifies the user, interpreter, Worker module, IPC argument, and process creation time; a declared different deployment is refused. Timeout or changed process identity reports failure. Stop its Front first so that another supervisor cannot restart it.

Exhausted startup/IPC retry budgets publish `FAILED`, then retry after 30 seconds. Consecutive failures back off through 60, 120, 240, and at most 300 seconds. Success ends that cooldown cycle; stopping the service cancels background recovery. Permanent configuration, protocol, identity, and Runtime-load errors require correction and explicit reload. Automatic recovery does not replay failed requests or establish that OS memory pressure has been resolved.
