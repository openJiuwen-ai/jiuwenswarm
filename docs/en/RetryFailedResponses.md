# Retry a failed response

In single-agent chat, a failed model request can show **Retry** below its error message. Retry sends the captured request again with a new request ID. It preserves the original prompt, selected model, persisted attachments and request settings, even if the current input selections have changed.

Only the latest submitted turn is eligible. The button is disabled while the conversation is processing, and rapid clicks cannot create concurrent attempts. A repeated failure offers another retry. A successful response removes the retry action. The original user bubble stays in place while the new response streams in. Retry sets the existing server option `log_as_user: false`, so reloading history does not add a duplicate prompt. A partial answer remains visible beside its failure; recovery starts a separate answer rather than appending to the unfinished text.

Retry is offered for correlated server `chat.error` events. A rejected or timed-out initial submission is not offered whole-request retry because its acceptance and history state may be unknown.

Whole-request retry is unavailable after tool work, or for team workflows, SwarmFlow and goal steering, because repeating these requests could repeat actions. This feature does not rewind external systems or resume from a tool checkpoint.

Request snapshots are local to the current page. Reloading the page or restoring conversation history can make retry unavailable; manually resend the prompt in that case. Snapshots expire after 30 minutes, are replaced on a new submission in the same session, and are bounded to 40 entries across sessions. Deleting a session clears its snapshot.

## Development checks

Run `npm run test:chat-retry` from `jiuwenswarm/channels/web/frontend`.

For a manual check, configure a local OpenAI-compatible mock endpoint and use Agent mode. Return HTTP 503 until an error appears. Click Retry twice rapidly and confirm only one new `chat.send` request is sent (model SDK HTTP retries may cause multiple HTTP requests). Keep returning 503 to check repeated failure, then switch the mock to a valid streaming response and retry again. Check that the response appears and the conversation becomes idle.

Also test an attachment, changing the selected model after failure, switching sessions, and reloading the page. Use mock providers for these tests; real model API access is unnecessary.
