# One-shot Process CLI machine execution

`jiuwenswarm-process --run-json FILE` (noninteractive) and
`jiuwenswarm-process --run-jsonl` (duplex) execute one single-root-Agent request
through `InProcessRuntimeClient` and the shared Runtime Public API. It does not
start Gateway, AgentServer, a WebSocket listener, a REPL worker, or a resident
stdio service. Runtime tools may launch their usual short-lived subprocesses.

One invocation owns one Runtime lifecycle. Persistent Session history can outlive
the process; continuing it starts a **new** CLI invocation with `session_id`.
Closing Runtime releases execution resources, not persisted Session history.

## Input

Read-only queries use `--query-json FILE|-`; see [one-shot query commands](#one-shot-query-commands).
Python and TypeScript hosts are documented in [SDK integration](../../sdks/README.md).

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
Unhandled task/async-generator failures observed during asyncio shutdown are
reported as `asyncio_shutdown`, rather than a successful result plus a log.
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

Signal handling covers initial input, Runtime execution and shutdown. A first
signal requests cancellation; repeated graceful signals do not interrupt an
already-running cleanup step. A signal received during cleanup is reflected in
the terminal result unless an earlier failure already determines the outcome.
After the terminal outcome is frozen for publication, late graceful signals
do not change its exit code. Signal handlers are restored on return.

The bounds above are **cooperative**, not a hard process watchdog: code that
blocks the event loop, suppresses cancellation, or stops draining a full output
pipe can prevent graceful progress. A broken output pipe is detected on write;
there is no heartbeat written solely to probe a reader. The host must enforce
an overall deadline, drain both outputs, and treat forced termination or a
missing/malformed terminal record as an incomplete invocation. A closed output
stream is not retried, since a JSONL record may already be partially delivered.

## Noninteractive interaction boundary

`--run-json FILE|-` remains noninteractive. Asking questions, plan approval and
harness activation observations produce `INTERACTION_REQUIRED`, followed by
cancellation and cleanup. Its stdin input still requires EOF. No answer is
inferred, auto-approved or supplied by reading the terminal.

## Duplex interaction within one command

An SDK can instead launch `jiuwenswarm-process --run-jsonl` with piped stdin,
stdout and stderr. This is a one-command JSONL protocol, **not JSON-RPC** or a
resident app-server. There is exactly one `run` per process. A second run,
unrelated Session command or configuration override is not accepted.

1. Write the same `run` object shown above, followed by a newline; flush it.
   Execution starts without waiting for stdin EOF.
2. Read stdout and stderr concurrently. An event whose `payload.event_type` is
   `run.started` identifies the prepared Session. An event whose
   `payload.event_type` is `interaction.requested` contains an opaque
   `payload.interaction_id` and the original Runtime card in
   `payload.interaction`. The underlying Runtime question is also emitted as an
   observation: answer the correlated notice, not both records.
3. Send an `answer` for that card, or send `cancel` to end the command. Repeat
   for subsequent interactions in this same run.
4. Keep stdin open if answers or cancellation may be needed. Wait for the single terminal
   `result` and process exit. Neither another command nor closing stdin is
   required for a normally completed command to exit.

For a question with the option `ALPHA`, an answer line is:

```json
{"schema_version":"0.1","type":"answer","request_id":"sdk-run-001","session_id":"<session_id from output>","interaction_id":"<opaque token from interaction.requested>","answers":[{"question":"<question from the card>","selected_options":["ALPHA"],"custom_input":""}]}
```

Use the card's actual question and option values. The outer `request_id` always
identifies the initial run; it is not the internal Runtime question ID. Both
`session_id` and the still-pending opaque `interaction_id` must match. A token
is consumed once. Stale, duplicate, foreign or premature answers fail the
command and cancel its owned work instead of being delivered to another turn.
The CLI copies source, approval metadata and execution bindings from the
observed Runtime card and original request; the caller cannot override them.

To cancel, send:

```json
{"schema_version":"0.1","type":"cancel","request_id":"sdk-run-001"}
```

`session_id` is optional for cancellation, allowing cancellation during startup.
If supplied, it must match. Cancellation targets only this invocation's owned
Session, including its active answer continuation, and returns `CANCELLED` with
exit code `130`. Clean EOF on duplex stdin after a complete run record closes
only the input direction: execution can finish if no answer is pending. If an
interaction is already pending, or appears later, EOF follows the cancellation
and cleanup path with `INPUT_CLOSED` and exit code `130`. This also applies after
an answer: its continuation may finish, but another unanswered question cannot
wait forever. EOF never implies permission approval. To cancel immediately,
send `cancel` before closing input rather than relying on EOF. This stage-5
behavior replaces stage 4's unconditional cancellation on duplex EOF; framing
and schema `0.1` are unchanged. The request deadline includes time waiting
for the caller's answer. Malformed control input fails with exit code `2`.

### Exit matrix

| Condition | Terminal / exit | Resource behavior |
| --- | --- | --- |
| Completed stream, stdin open or cleanly half-closed | `completed` / `0` | Close streams, Session execution resources and Runtime; exit without another command |
| Fatal Runtime error or dependency `SystemExit` | `failed` / `1` | Stop consumption, cancel owned work, attempt every cleanup step |
| Missing completion evidence | `INCOMPLETE_RUN` / `1` | Never infer success from a stream EOF or answer acknowledgement |
| Request deadline | `TIMEOUT` / `124` | Cancel and clean up, including while waiting for an answer |
| Graceful signal or explicit cancel | `CANCELLED` / `130` | Cancel only the owned Session/request; preserve an earlier failure |
| EOF with a pending or subsequent unanswered interaction | `INPUT_CLOSED` / `130` | No synthetic answer or approval; cancel and clean up |
| Missing run, malformed input, partial JSONL at EOF | `failed` / `2` | No Runtime startup for invalid initial input; cancel owned work for invalid controls |
| Cleanup/shutdown failure without an earlier error | `SHUTDOWN_FAILED` / `1` | Attempt remaining cleanup steps and report their names |
| Output write/flush fails | No deliverable terminal / nonzero | No retries; cancel owned work and clean up |

Persistent Session history is not deleted by any of these exit paths. A future
invocation may resume persisted history, but cannot answer an in-memory
interaction belonging to a process that has already exited.

Each input line is a UTF-8 JSON object, terminated by LF or CRLF, at most 1 MiB
excluding the line ending. Blank, incomplete, duplicate-key and unknown-field
records are rejected. Flush each line. Piped stdin and redirected files are
supported; this entrypoint does not implement an interactive console editor.
Do not combine `--run-jsonl` with other CLI flags. Workspace, model and Agent
settings belong in the initial run document.

Typed Runtime question, permission, plan-confirmation and evolution approval
cards use the same answer path; their actual decisions remain Runtime-owned.
Hard denials are not promoted to approvals. Legacy Harness activation remains
unsupported (`INTERACTION_UNSUPPORTED`), with cancellation and cleanup, rather
than being translated into a different approval type.

An answer acknowledgement or answer-stream EOF is **not** run completion.
Runtime marks synthetic finals as `suspended` or `completed` internally;
an interrupt's suspended UI flush is not completion evidence. If all streams
end with only that tail and an acknowledgement, the CLI returns `INCOMPLETE_RUN`.
An actual completed final may be empty: success does not depend on assistant
text or a tool result. These internal marks do not change the wire protocol.
For plain interrupted rounds the shared Adapter drains the previous output
owner before dispatching the answer, preventing injection into a finishing
lease. Answers are dispatched once, never automatically replayed.
Runtime may continue output on the original stream or on the answering stream;
the CLI drains both, retaining the same external run/Session identity and output
sequence. It stops its input reader and closes all streams before Session and
Runtime cleanup, and still emits the terminal result only after cleanup.

## One-shot query commands

`jiuwenswarm-process --query-json FILE` or `--query-json -` reads one bounded
UTF-8 document (stdin requires EOF), starts one Runtime, calls its public query
API, closes Runtime and exits. It never creates/resumes an Agent or changes the
active Session/model/mode/permission/MCP configuration. Do not mix query and run
flags or legacy flags. This is not a batch, JSON-RPC service, or resident worker.

```json
{
  "schema_version": "0.1",
  "type": "query",
  "request_id": "catalog-001",
  "operation": "session.list",
  "params": {"limit": 20, "offset": 0},
  "timeout_seconds": 30
}
```

| Operation | Params | Runtime result in `data` |
| --- | --- | --- |
| `session.get` | Required `session_id` | `session` summary or `null` for missing/foreign/non-single-Agent Sessions |
| `session.list` | Optional `limit` (1–200), `offset` (nonnegative) | `sessions`, `total`, `limit`, `offset` |
| `model.list` | None | Configured, credential-free model catalog |
| `model.resolve` | Required `requested` | One public model descriptor; not a live model connectivity test |
| `mode.list` | None | Four supported single-Agent modes and capabilities |
| `mode.resolve` | Required `requested` | Descriptor; Runtime retains supported legacy aliases |
| `permission.get` | Optional `session_id` | Host snapshot or owned Session overlay; no approval or policy mutation |
| `mcp.validate` | Required `references` array | Local readiness/missing reference facts, without connecting or changing MCP configuration |

`workspace` is optional and follows the existing path-resolution contract.
Unknown operations/params and malformed types fail with exit 2 before Runtime
startup. Domain validation errors retain Runtime error codes with safe messages.
Session ownership is fixed to `process_cli`; a caller cannot pass `channel_id`.
MCP validation returning missing/not-ready is a successfully executed query, not
evidence that references are usable. Inspect `data`; no config secrets are exposed.

Queries emit exactly one `type=query_result`, schema `0.1`, `sequence=0`, and
`session_id=null`. Fields are `request_id`, `operation`, `status`, `exit_code`,
`data`, and `error`. A successful result needs no invented Session ID. Read `data`
only on `status=completed`. The existing chat `event`/`result` schema is unchanged.
Early invalid-input failures may have `operation=null`. Exit and cleanup failure
semantics match machine runs; a cleanup failure cannot report successful queries.
