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
not claimed to be published.

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
Do not also answer the raw Runtime interaction event. Without a callback,
permission cards offering `reject` are rejected automatically and the Agent
continues. Other interactions raise `InteractionRequired` and cancel the run.
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

For structured results, send an object-root JSON Schema in `output_schema` and
read `result["output_json"]`. Invalid model output fails the run. Use `max_turns`
to bound model calls and `max_budget_usd` to bound reported USD cost. A model
without cost data causes a budgeted run to fail closed. One model call may
cross a cost bound before Runtime can stop it.

To expose an async host tool, put its name in `agent.tools`, declare it in
`host_tools`, and pass `on_tool_call`. The callback receives a
`host_tool.requested` event with `name`, `arguments`, and `call_id` in `payload`;
its returned JSON value is sent to the waiting child as `tool_result`.

```python
async def lookup(event):
    key = event["payload"]["arguments"]["key"]
    return {"value": host_database[key]}

result = await client.run({
    "input": "Look up my item",
    "agent": {"name": "lookup-agent", "instructions": "Use lookup.", "tools": ["lookup"]},
    "host_tools": [{"name": "lookup", "description": "Look up a host item",
                    "input_schema": {"type": "object", "properties": {
                        "key": {"type": "string"}}, "required": ["key"]}}],
}, on_tool_call=lookup, deadline_seconds=180)
```

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
`onToolCall` implements a declared host tool and may return a JSON value or a
promise of one. Rejected callbacks become tool errors.
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
  process. Cross-process cancel/answer is not provided. The Process CLI holds
  an exclusive Session lock until Runtime cleanup finishes, so another process
  receives retryable `SESSION_BUSY`. Resend the same Agent definition on resume;
  changing or omitting it returns `AGENT_DEFINITION_SESSION_CONFLICT`.

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
The opt-in acceptance case for structured output, limits, host tools and
unattended permission is
`JIUWENSWARM_REAL_MODEL_TEST=1 python -m pytest -o addopts='' tests/system_tests/test_process_cli_sdk_p0_live.py`.
