# One-shot Process CLI machine execution

`jiuwenswarm-process --run-json FILE` executes one single-root-Agent request
through `InProcessRuntimeClient` and the shared Runtime Public API. It does not
start Gateway, AgentServer, a WebSocket listener, a REPL worker, or a resident
stdio service. Runtime tools may launch their usual short-lived subprocesses.

One invocation owns one Runtime lifecycle. Persistent Session history can outlive
the process; continuing it starts a **new** CLI invocation with `session_id`.
Closing Runtime releases execution resources, not persisted Session history.

## Input

```sh
jiuwenswarm-process --run-json request.json
# Alternatively, write one UTF-8 document to stdin, then close stdin (EOF):
jiuwenswarm-process --run-json - < request.json
```

`request.json`:

```json
{
  "schema_version": "0.1",
  "type": "run",
  "request_id": "sdk-run-001",
  "input": "Explain the purpose of this project without changing files.",
  "mode": "agent.code.normal",
  "workspace": {
    "cwd": "/path/to/project",
    "project_dir": "/path/to/project",
    "trusted_dirs": ["/path/to/project"]
  },
  "timeout_seconds": 120
}
```

Only `schema_version`, `type`, and nonempty `input` are required. The input is
one complete JSON document, at most 1 MiB, not a stream of commands. Duplicate
keys, unknown fields, unsupported versions/modes and non-finite numbers fail
before Runtime starts. Do not combine `--run-json` with legacy execution flags;
put the settings in the document. Legacy prompt, `--output` and REPL entrypoints
retain their existing formats and behavior.

Supported root modes: `agent.code.normal`, `agent.code.plan`,
`agent.work.normal`, `agent.work.plan`. Team/Workflow/AutoHarness roots are not
accepted. New Sessions default to `agent.code.normal`. Supplied `session_id`
must identify an existing Process CLI Session; missing and foreign Sessions are
not adopted or deleted. An omitted mode inherits that Session's mode.

Optional `agent` uses the existing Agent definition contract, for example:

```json
{
  "name": "project-reviewer",
  "instructions": "Explain findings concisely. Do not modify files.",
  "model": "configured-model-name"
}
```

Attach this object as the request's `agent` field. Omit `model` to use the
configured selection. Definitions are validated and executed by Runtime, not
loaded into a second Agent engine. Current Runtime rejects custom work-mode
Agents and explicit tool allowlists; those errors are preserved, not silently
downgraded. Available skills and model names still come from local Runtime
configuration. The request is not a configuration-file override mechanism.
An inline definition is invocation-scoped: send it again to execute the same
custom definition when resuming history in a later process.

Workspace paths are resolved relative to the launching directory **before**
changing `cwd`. `cwd` controls process execution; `project_dir` remains a
separate project binding. Omitted workspace fields on resume are left to
Runtime's persisted-binding rules. Only persisted facts can be inherited:
today a separately supplied per-invocation `cwd` is not part of the public
Session descriptor; supply it again when it differs from the project root.

## Output and lifecycle

Stdout contains only UTF-8 JSONL, with zero-based consecutive `sequence`, a
stable external `request_id`, and the resolved `session_id`. Python/native
dependency diagnostics and inherited tool stdout go to stderr. The adapter
flushes each event immediately rather than retaining the event stream.

Records are schema `0.1` `event` observations followed by exactly one terminal
`result`. Read the result's `status`, `exit_code`, `output`, `usage`, and `error`;
`chat.final` by itself is **not** the command outcome. Usage summaries are not
added twice to per-call usage; tool-local errors do not automatically mark the
whole run failed. An empty or incomplete Runtime stream cannot report success.

The terminal result is written only after stream closure, Session cleanup,
Runtime closure and asyncio shutdown. Cleanup exceptions prevent success and
are recorded as cleanup step names; an earlier execution error is preserved.
The per-step cooperative cleanup deadline is five seconds, separate from the
request deadline. `timeout_seconds` bounds Runtime start/preparation/execution,
not input reading, module imports or cleanup. A parent SDK should also enforce
its own wall-clock deadline and drain both stdout and stderr concurrently.

| Exit code | Meaning |
| --- | --- |
| `0` | Completed and cleaned up |
| `1` | Runtime/interaction/incomplete-stream/cleanup/output failure |
| `2` | Invalid machine input or argument combination |
| `124` | Request deadline expired |
| `130` | Interrupted/cancelled command |

On a broken stdout pipe, the command cancels owned work and cleans up; it cannot
deliver a final record to a reader that has disconnected. Likewise, a force kill
or OS crash cannot guarantee a terminal record. Treat missing terminal output
as incomplete, never as success. SIGINT, SIGTERM and Windows CTRL_BREAK (when
delivered to the CLI) use the same cooperative cancellation path; Windows
TerminateProcess is a force kill, not a graceful signal.

## Interaction boundary in this stage

This is not JSON-RPC and stdin is not yet duplex. Asking questions, plan approval
and harness activation observations produce `INTERACTION_REQUIRED`, followed by
cancellation and cleanup. The CLI does not auto-approve or bypass Runtime
permissions. SDK-driven interaction answers are a later stage, within the same
one-invocation/one-process lifetime, not a resident app-server.
