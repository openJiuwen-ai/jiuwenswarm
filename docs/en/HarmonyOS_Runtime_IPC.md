# Testing HarmonyOS Runtime IPC

AgentServer defaults to `front_loopback` in HarmonyOS runtimes. The Front listens on an ephemeral IPv4 `127.0.0.1` port; the Worker connects and authenticates before loading Runtime.
The Worker creates no Unix socket or resident ownership lock and exits after the Front disconnects.
Ordinary Linux/macOS installations still default to Unix sockets and Windows to Named Pipes.

## Defaults and explicit overrides

After updating the HarmonyOS package with this branch, start it normally. No additional `JIUWENSWARM_RUNTIME_IPC` setting is required.
Detection preserves the existing rules: `JIUWEN_HARMONY_RUNTIME=1`, `sys.platform == "ohos"`, or a PATH directory containing both `/data/app/` and `/hnp/`.
This also covers HNP Python runtimes that report Linux.

An explicit setting is only needed to test this transport on an ordinary desktop:

```powershell
$env:JIUWENSWARM_RUNTIME_IPC = 'front_loopback'
jiuwenswarm-agentserver
```

An explicit `JIUWENSWARM_RUNTIME_IPC=resident` overrides the HarmonyOS default. Unset, empty or whitespace-only values use the platform default; unknown values fail.
Logs identify `runtime_ipc=front_loopback`, the failing stage and errno; there is no automatic fallback to Unix sockets.
Connection parameters and a one-use token travel through the existing private Worker stdin payload. No fixed IPC port or command-line credentials are required.

## Local checks

With the repository's test dependencies installed, run:

```text
python -m pytest tests/unit_tests/agentserver/test_front_loopback_ipc.py --no-cov
```

Coverage includes invalid authentication, slow candidates, timeouts, failed/cancelled startup, real Worker subprocess startup/restart/exit, and Worker exit after a real Front is killed.
Request/push delivery and dispatcher drain use a deterministic Runtime replacement and do not call a model.
Existing business messages and their 32 MB frame limit are unchanged.

## HarmonyOS packaging and acceptance

Update the HarmonyOS packaging repository to this implementation, then rebuild both `python-jiuwenswarm.hnp` and `pyc_bundle.tar` so an old `.py_src` cannot override it.
Build, inject and sign the HAP, record the bundled revision, and verify at least 20 device cold starts, crash recovery, Front termination, request/stream/cancel delivery and graceful shutdown.
Observe actual Worker exit, released ports and the absence of Runtime socket files.
Windows repository tests do not establish HarmonyOS device, POSIX or HNP/HAP compatibility.

Rollback must restore the business revision, HNP and overlay together, and remove `JIUWENSWARM_RUNTIME_IPC`.
