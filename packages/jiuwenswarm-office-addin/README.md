# JiuwenSwarm Office add-in

The PowerPoint task pane: it attaches the open presentation to a JiuwenSwarm
conversation and runs the conversation's PowerPoint tools in the document.
The backend half is the `office-powerpoint` client app
(`jiuwenswarm/office/`); its `powerpoint/tools.json` and `profile.json` are
the tool contract both halves read (`@contract`).

This package holds the pane's protocol core so far. The task pane UI, the
PowerPoint tool runtime, the manifest and the production build follow in later
changes.

## Modules

| Module | Owns |
|---|---|
| `gatewayClient` | The WebSocket to the Gateway: requests, events, reconnects. A refusal rejects with its `code`. |
| `clientAttachment` | `client.attach` / `client.renew` of one document to one conversation, and the client tool calls of that conversation: each runs once, and the finished ones are answered in one `chat.send` (`source: "client_tool_interrupt"`) while the turn is not running. An answer goes only to the conversation that asked for it. A failed attach tries again; a window another window displaced answers its calls as run there. The renews run on `workerTimer`. |
| `workerTimer` | A timer that keeps time while the pane is hidden: the wait runs in a dedicated Worker (`timerWorker`), as a hidden pane's own timers are throttled and could let the lease lapse. |
| `documentIdentity` | The document's GUID (`jiuwenswarm.doc_id` in the document settings), which is the attach `resource.key`, and its display name. |
| `presentationContract` | The attach declaration. Its `supported_tools` are the tools in the contract, implemented by the injected tool runtime, and supported by the running PowerPoint. |
| `powerPointToolDispatcher` | Runs a call in the injected tool runtime, writes through `hostWriteQueue`, and shapes the answer: text (and a PNG for renders) for the model, latency telemetry in `data`. A call the host has not finished in 280 s is answered with status `timeout`; a write keeps the queue until the host settles it, and a write that timed out while queued never runs. A host result that does not serialize is answered as an error. |
| `hostWriteQueue` | The one critical section for document writes, first come first served. |
| `presentationSessionFacade` | `session.create` (`client_app: "office-powerpoint"`), `client.conversations` (the conversations of this document), `chat.send`, `chat.interrupt`, `media.persist`. |
| `clientToolOutcome`, `toolResultBudget` | Pure shaping of a write's mutation record and of the result size. |

Approvals are the backend's: a write the user's permission rules ask about
pauses on the backend before the pane is ever called.

## Checks

```
npm ci
npm run typecheck
npm test
```
