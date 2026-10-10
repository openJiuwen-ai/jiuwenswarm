# Independent JEV classifier

An async Python component with one public classification operation:

```python
behavior = await classifier.classify(context="...", messages=["...", "..."])
# Exactly "INTERRUPT" or "APPEND" on success; explicit exceptions on failure.
```

The package depends on `httpx` and Python 3.11+, with no JiuwenSwarm, agent-core,
native-harness, database or queue dependency. It reads only the supplied input.
The caller assembles context and applies delivery behavior.

Keep this package at `packages/jev-classifier` and install it on its own.
JiuwenSwarm integration, including when to call it and how to apply the label,
stays outside this package.

## Install and call

From the repository root:

```shell
python -m pip install ./packages/jev-classifier
```

Supply the endpoint, model and credential explicitly. The library does not read
environment variables or application configuration itself. The example below
uses caller-owned environment variables and an HTTP client for connection reuse.

```python
import asyncio
import os
import httpx
from jev_classifier import ClassifierConfig, JevClassifier, JevHttpConfig, JevHttpProvider

async def main():
    config = JevHttpConfig(
        endpoint=os.environ["JEV_ENDPOINT"],  # Full /v1/systemone or /v1/decisions URL.
        model=os.environ["JEV_MODEL"],
        api_key=os.environ["JEV_API_KEY"],
    )
    async with httpx.AsyncClient() as client:
        classifier = JevClassifier(
            JevHttpProvider(config, client=client),
            ClassifierConfig(timeout_seconds=2.0),
        )
        behavior = await classifier.classify(
            context="The user requested a revenue report. I am calculating totals from input v1.",
            messages=["The user supplied corrected input v2. Use it for the current report."],
        )
        print(behavior)

asyncio.run(main())
```

An equivalent runnable example is in `examples/classify.py`. It makes a real API
call when run with those variables set. No live API call is made during tests.

## Input and output contract

| Input | Contract |
| --- | --- |
| `context` | Nonempty text describing one recipient's relevant task, current activity, requirements and conversation. Callers may serialize structured context into this string. There is no required runtime schema. |
| `messages` | Nonempty list or tuple of nonempty strings, in caller-supplied order. The sequence is copied before awaiting the provider. |

The output is one label for the **whole batch**:

- `INTERRUPT`: the messages require changing ongoing work before it continues.
- `APPEND`: the messages can be incorporated without interrupting ongoing work.

A compatible addition or a task explicitly requested for later usually means
APPEND. A correction invalidating the current activity usually means INTERRUPT.
If context explicitly describes an idle recipient, there is no ongoing work to
interrupt, so the prompt calls for APPEND. This is a semantic judgment; the module
still calls JEV when invoked. It does not implement an idle bypass.

Use one call per recipient context. If individual messages require independent
decisions, call separately. A batch decision does not identify messages to discard
or rewrite. Caller ordering is preserved but does not establish instruction
authority. Include relevant source/role information in context or message text
when needed; sender claims alone do not establish authority.

There are no required recipient IDs, lifecycle enums, snapshot versions or native
runtime objects. Jiuwen retains its own correlation IDs and checks whether a
result still applies to the work before acting on it.

## Failures and limits

| Exception | Meaning |
| --- | --- |
| `InvalidInputError` | Wrong input type, empty message, or invalid Unicode. |
| `InsufficientContextError` | Context is blank. This is a subtype of InvalidInputError. |
| `InputTooLargeError` | More than 64 messages or over 32,768 UTF-8 bytes by default. |
| `InvalidResponseError` | Invalid typed answer, invalid probabilities, malformed JSON or response over the configured size. |
| `JevTimeoutError` | Provider call exceeded the total deadline or had a transport timeout. |
| `ProviderError` | HTTP failure or unexpected injected-provider failure. |

All these inherit from `JevError`. Invalid configuration raises `ValueError` at
construction. Caller cancellation propagates as `asyncio.CancelledError`.
Failures never become APPEND. Jiuwen chooses fallback behavior separately.

The input size counts the compact UTF-8 JSON object containing `context` and
`messages`, including its field names and escaping. It excludes the fixed prompt
and model name. Oversized input is rejected before inference, never truncated.
`ClassifierConfig` controls deadline, byte limit and message count. The deadline
includes HTTP connection setup and response streaming. Providers must cooperate
with async cancellation. The built-in HTTP provider uses a 65,536-byte decoded
response limit by default, configured through `JevHttpConfig`.

Blank context can be rejected mechanically. Nonempty but misleading or incomplete
context cannot be reliably detected by this interface; context quality and model
judgment remain evaluation concerns. The module has no access to hidden runtime
state to fill in missing information.

## Provider contract

The built-in provider uses the typed-choice wire format already used by this
repository: `model`, object `state`, and `questions.action` with two criteria.
It expects `answers.action` to contain `type="choice"`, a valid `choice`, numeric
`confidence`, and probabilities for exactly INTERRUPT and APPEND. Probabilities
must be in [0, 1], sum to one within 1e-5, and agree with the selected choice.
Additional provider metadata is ignored.

The validated choice is returned directly. There is no probability threshold that
silently converts INTERRUPT into APPEND. Confidence is validated but does not
change the label. There are no automatic retries or redirects. The module does
not log credentials, input text or raw response bodies. Injected HTTP clients stay
caller-owned; otherwise a temporary client is created and closed for each call.

An SDK-backed provider can implement `DecisionProvider` without changing callers:

```python
class MyProvider:
    async def decide(self, *, context: str, messages: tuple[str, ...]) -> str:
        # Call a configured provider/SDK and validate its response here.
        ...
```

Use `JevHttpProvider` through `JevClassifier`, which supplies input validation and
the total deadline. The release-branch agent-core JEV client identified in the
handoff remains an optional future adapter; it is not a dependency of this package.
The current HTTP adapter's live service compatibility has not yet been verified.

## Jiuwen integration handoff

Jiuwen decides **when** to invoke this module, supplies context, and maps the label
to its runtime operations. `INTERRUPT` is not automatically an `abort` command;
`APPEND` does not dictate a particular queue.

| Source | Requested policy for Jiuwen |
| --- | --- |
| Private team steer/abort | Invoke JEV, subject to agreed idle precedence. |
| Private team followup | Bypass JEV. |
| Idle recipient | Direct delivery; scope/precedence remains to be settled by Jiuwen. |
| Broadcast | Invoke JEV using each recipient's context. |
| Chat @mention | Invoke in any state; exact mention eligibility remains to be clarified. |
| Direct typed-user input | Preserve manual delivery controls; bypass automatic JEV judgment. |
| Voice interaction and voice-agent/STT | Invoke at the intended delivery boundaries; partial/final transcript policy remains open. |

Jiuwen also owns sender-requested action mapping, successful-steer followup
clearing, stale-result handling, and error fallback. This package exports no
send, pause, abort, resume, clear-queue or context-collection operation.

## Validation

From the repository root, using a Python environment with this package's test extra:

```shell
python -m pip install -e "./packages/jev-classifier[test]"
python -m pytest -c packages/jev-classifier/pyproject.toml packages/jev-classifier/tests
```

Tests exercise input validation, typed-response validation, batch preservation,
deadlines, cancellation, HTTP failures, response limits and client ownership using
injected providers and mock HTTP transports. They do not measure model accuracy.

`examples/labeled_cases.json` contains proposed semantic cases for a separate live
evaluation, including the same update under different recipient contexts. Expected
labels and rationales are evaluation data and must not be included in model input.

