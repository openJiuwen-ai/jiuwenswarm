# One-shot Process SDKs (schema 0.1)

These are thin **local child-process clients**, not another Agent engine or an
app-server. Install JiuwenSwarm and configure its model first. Each SDK call
creates one `jiuwenswarm-process`, talks over its pipes, waits for Runtime cleanup
and process exit, and retains no live Runtime. Calls are not retried automatically.

Existing TUI/Web/IM/Gateway/AgentServer entrypoints are unchanged. The SDKs do not
import JiuwenSwarm, openjiuwen, Server, Gateway, or a network client.

## Install from this checkout

```sh
python -m pip install ./sdks/python
cd sdks/typescript
npm ci
npm run build
npm pack
```

Python requires 3.11+; TypeScript targets Node.js 22+. Neither has runtime
npm/Python dependencies. Publishing to PyPI/npm is separate; these packages are
not claimed to be published. `uv.lock` is unchanged.

## Python

```python
import asyncio
from jiuwenswarm_sdk import Client

async def main():
    client = Client()
    modes = await client.query("mode.list", deadline_seconds=60)
    print(modes["data"])
    result = await client.run({
        "input": "Explain the project without changing files.",
        "agent": {"name": "reviewer", "instructions": "Be concise; do not modify files."},
        "workspace": {"cwd": "/path/to/project"},
        "timeout_seconds": 120,
    }, deadline_seconds=180)
    print(result["status"], result["output"])

asyncio.run(main())
```

For a source checkout, supply
`Client([python_executable, "-m", "jiuwenswarm.channels.process_cli.main"],
cwd=checkout, env={"JIUWENSWARM_DATA_DIR": isolated_data_dir})`.
Use an absolute executable from the environment containing JiuwenSwarm. `command`
is argv, not a shell string; arguments with spaces need no manual quoting.

`on_event` is an async callback receiving versioned event records. An optional
async `on_interaction` receives only `interaction.requested` and returns the
existing `answers` array. Render `event["payload"]["interaction"]` questions and
options to the actual host/user. The SDK copies the opaque `interaction_id`,
command identity and Session identity to the answer envelope automatically.
Do not also answer the raw Runtime interaction event. No callback means
`InteractionRequired`, cancellation and cleanup, **never auto-approval**.
ASK remains a host decision; DENY is not widened.

The current question-answer shape is an object with `question`,
`selected_options` (an array of offered option **values**) and `custom_input`;
permission cards also require their original `card_id`. For example, after the
host user explicitly chooses an offered option:

```python
return [{
    "question": question["question"],
    "selected_options": [user_selected_option_value],
    "custom_input": "",
    # "card_id": question["card_id"]  # include for a permission card
}]
```

Do not replace these fields with `{"answer": "yes"}`. The SDK preserves the
Runtime answer shape instead of guessing permission/Plan decisions or flattening
different interaction types into a boolean.

Pass an `asyncio.Event` as `cancel=` and set it to cancel the current run. Cancelling
the calling task or exceeding `deadline_seconds` also closes that child. Answers
and cancellation never launch another command. Resume persisted history in a
**new** `run` containing `session_id`; resend invocation-scoped Agent definitions
and a distinct `cwd` when needed.

## TypeScript

```typescript
import { Client } from "@jiuwenswarm/process-sdk";

const client = new Client();
const models = await client.query("model.list", {}, { deadlineMs: 60000 });
const controller = new AbortController();
const result = await client.run({
  input: "Explain the project without changing files.",
  workspace: { cwd: "/path/to/project" },
  timeout_seconds: 120,
}, {
  signal: controller.signal,
  deadlineMs: 180000,
  onEvent: event => console.log(event.event_type),
});
console.log(result.status, result.output);
```

`new Client({command: [pythonExecutable, "-m",
"jiuwenswarm.channels.process_cli.main"], cwd: checkout, env: {...}})` supports a
source checkout. `onInteraction` has the same semantics as Python; it returns or
resolves an answers array. `AbortController.abort()` cancels the current run.
No JSON-RPC endpoint or persistent stdio service is involved.

## Result and failure contract

- Runtime failures return the structured non-success result; inspect `status`,
  `exit_code` and `error`. Queries return `query_result`, not a chat result.
- Success requires one terminal record **and matching OS exit status**, matching
  schema/request/Session identity and contiguous sequence. EOF, `chat.final`,
  a mismatched exit code or duplicate terminal records cannot imply success.
- Transport, invalid protocol, callback and host deadline failures raise. No
  automatic retries occur. Force kill or broken pipes may prevent a final record.
- Both output pipes are drained. Default maximum output record size is 8 MiB,
  configurable; input is bounded by the CLI's 1 MiB limit. No event history is
  retained. TypeScript bounds pending delivery to 256 records and fails closed
  for a slow host. Python applies pipe backpressure.
- Set a host deadline for untrusted/hung callbacks. Host callbacks own their UI
  resources; the SDK cannot undo arbitrary callback side effects. Consider
  secrets before logging permission payloads, tool output or stderr.
- Runtime execution timeout excludes import, input and shutdown. Host deadline
  covers command I/O and exit; bounded cleanup can extend it. Default shutdown
  grace is 30 seconds. Normal run cancellation uses protocol control. Force
  termination after grace is a failure fallback, not proof of Runtime cleanup.
- Session queries return owned single-Agent metadata, not handles to another
  process. Cross-process cancel/answer and concurrent same-Session scheduling
  are not provided. Serialize calls modifying the same Session.

## Tests

```sh
python -m pytest -o addopts='' sdks/python/tests tests/unit_tests/process_cli/test_query.py
cd sdks/typescript
npm test
```

The shared real-subprocess fixture tests UTF-8 fragmentation, large stderr,
framing, interactions, cancellation, callbacks, deadlines and process exits.
It is **not** a real-model E2E. Separately verify SDK -> CLI -> shared Runtime in
an isolated configured environment, including real tool effects and cleanup.
