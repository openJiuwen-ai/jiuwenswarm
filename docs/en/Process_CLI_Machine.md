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
4. Keep stdin open while the run is active. Wait for the single terminal
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
exit code `130`. EOF on duplex stdin before completion means caller disconnect:
it follows the same cleanup path with `INPUT_CLOSED` and exit code `130`. It
does not imply permission approval. The request deadline includes time waiting
for the caller's answer. Malformed control input fails with exit code `2`.

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
Runtime may continue output on the original stream or on the answering stream;
the CLI drains both, retaining the same external run/Session identity and output
sequence. It stops its input reader and closes all streams before Session and
Runtime cleanup, and still emits the terminal result only after cleanup.
