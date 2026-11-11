# JiuwenSwarm Office add-in

The PowerPoint task pane: it attaches the open presentation to a JiuwenSwarm
conversation and runs the conversation's PowerPoint tools in the document.
The backend half is the `office-powerpoint` client app
(`jiuwenswarm/office/`); its `powerpoint/tools.json` and `profile.json` are
the tool contract both halves read (`@contract`).

So far the pane chats, switches between the document's conversations, answers
the backend's approvals and questions, shows each turn's tool calls on a
timeline, runs the read-only PowerPoint tools, and builds and edits slides as
native PowerPoint objects (`presentation_apply_patch`), with an undo icon
under the reply of each turn that wrote. The slide XML tools (charts, masters, imports,
find and replace, connectors) follow in later changes.

## Support

The manifest requires PowerPoint JavaScript API 1.8, so the add-in loads only
in Microsoft 365 PowerPoint (Windows, Mac and the web). PowerPoint 2016, 2019,
2021 and LTSC (perpetual licenses) and PowerPoint on iPad are not supported.
The pane declares the tools the running PowerPoint can run
(`host_requirements` in `tools.json`); on 1.8, `presentation_apply_patch`
refuses the row rules, row heights and column widths of `update_table`, which
need 1.9.

## Undo this turn

PowerPoint folds an add-in's writes into one undo step of its own, so the pane
keeps its own undo. On the first write of a turn to a slide it exports that
slide (`exportAsBase64`) and notes the deck's slide order; a new slide needs
only its id, a moved one only the order. When the turn ends it hashes the
slides the turn left. The undo icon under the turn's reply undoes that
turn and every later one, the last first, inside the write queue: new slides
are deleted, changed and deleted ones are inserted again from their exports,
and the slides go back into their order. When a slide changed since the turn
(its hash differs, or it is gone), it asks in one line before it overwrites. The
slides put back have new ids, so the next message starts with a line telling
the agent to read the presentation again.

A patch the host fails part way is rolled back on its own: before each patch
the pane exports the slides it changes, and on the failure it puts them back
and removes the slides the patch made, so the deck is never left half changed.

Known limits: the undo lives in the pane's memory, so reloading the pane or
reconnecting (which replays the transcript) loses it; the pane keeps the last
10 turns or 64 MB of exports, whichever is less; changes to masters, the theme
and notes are not undone.

## Modules

| Module | Owns |
|---|---|
| `gatewayClient` | The WebSocket to the Gateway: requests, events, reconnects. A refusal rejects with its `code`. |
| `clientAttachment` | `client.attach` / `client.renew` of one document to one conversation, and the client tool calls of that conversation: each runs once, and the finished ones are answered in one `chat.send` (`source: "client_tool_interrupt"`) while the turn is not running. An answer goes only to the conversation that asked for it. A failed attach tries again; a window another window displaced answers its calls as run there. The renews run on `workerTimer`. |
| `workerTimer` | A timer that keeps time while the pane is hidden: the wait runs in a dedicated Worker (`timerWorker`), as a hidden pane's own timers are throttled and could let the lease lapse. |
| `documentIdentity` | The document's GUID (`jiuwenswarm.doc_id` in the document settings), which is the attach `resource.key`, and its display name. |
| `presentationContract` | The attach declaration. Its `supported_tools` are the tools in the contract, implemented by the injected tool runtime, and supported by the running PowerPoint. |
| `powerPointToolDispatcher` | Runs a call in the injected tool runtime, writes through `hostWriteQueue`, and shapes the answer: text (and a PNG for renders) for the model, latency telemetry in `data`. A call the host has not finished in 280 s is answered with status `timeout`; a write keeps the queue until the host settles it, and a write that timed out while queued never runs. A host result that does not serialize is answered as an error. |
| `powerPointReadTools` | The read-only tools on `PowerPoint.run`: `read_presentation_context`, `list_slide_shapes`, `read_slide_image` (960 wide by default), `verify_slides` and `verify_slide_visual`. Each call reads in a fixed number of batched syncs, however large the deck; when a shape's text cannot be read, the text is read one sync per shape. Theme palette, theme fonts and template body size are null until the OOXML reader lands. |
| `powerPointWriteTools` | `presentation_apply_patch` on `PowerPoint.run`: checks `base_structure_fingerprint` against the deck, fetches the patch's images, draws it, and answers with `structure_fingerprint_after`; a patch the host fails part way it rolls back (`rolled_back`). It keeps the design system the deck's last patch declared. Also the deck as the undo works it (export, insert, remove, order). |
| `presentationScenePatch` | Reads a patch (roles with their font floors, design-system tokens, exact fields) and draws it in one batch: every slide, shape, table and layout it names is checked before the first change; a failure after that says which slides landed. It tells the turn's undo what it is about to change before it changes it. `add_connector` is refused until the slide XML renderer lands. |
| `presentationAssetClient` | Fetches a published file a call inserts from its signed `/file-api/download` link and takes it only if its size and SHA-256 are the descriptor's. |
| `slideSnapshots` | The per-turn snapshots and the undo (see above). |
| `hostWriteQueue` | The one critical section for document writes, first come first served: tool writes, the end-of-turn hashes and undos. |
| `paneController` | The pane's state and the user's actions: opening the document's latest conversation (or a new one), sending (on the model picked under the message box, `chat.send` `model_name`), stopping, answering a question, switching and starting conversations, taking the document back from another window. It also holds the connection state (down, with the next attempt and when) and the outcome of an action the transcript has no place for, until dismissed. The shown conversation's history loads first, again on every attach (as after a reconnect); its live events wait for it. A turn that wrote closes its snapshots and offers its undo while no turn runs; after an undo the next message sent starts with a line saying which slides came back, which the transcript does not show. A `models.updated` reads the models again. |
| `conversationModel` | A pure reducer from a conversation's events to what the pane shows: the transcript, whether the turn runs, and the question it waits on (`chat.ask_user_question` with source `permission_interrupt`, `confirm_interrupt` or `ask_user_interrupt`, answered through `chat.send` as the web chat answers it). A tool call keeps what the model said it is for (`call_goal`) and its arguments. A turn that ran a writing tool ends with an entry naming the calls its undo takes back. The same reducer replays the history. |
| `crossDocumentMessages` | Reads the tasks this conversation sends to other documents (`session_send_message`) and the ones other documents send here (`cross_session_agent`, `session.message.updated`). |
| `view/*` | The DOM, as small components put together by `paneView`: `header` (icons only: history, new conversation, More with the transcript download; the top right corner stays free for the host's menu), `transcript` (messages; under a finished turn's reply, icons to copy it and, while offered, to undo it; dots while the agent works and nothing moves; and `activity`: each run of tool calls as a vertical timeline with live seconds, folded once done to one quiet line, the step's name or "查看过程", with no counts or failures, arguments shown only on request, one at a time), `tray` (above the composer: reconnect countdown, connecting, displaced with "continue here", failed with "retry", the outcome of the last action), `interaction` (the approval sheet pinned at the bottom: Esc rejects, Enter allows once but not within 350 ms of the sheet appearing; the agent's question card), `composer` (one row, Send and Stop share one button inside it, the model picker below; an Enter that picks an input method candidate does not send). A separate live region reads out replies, approvals and tray lines; the transcript is not one. No component moves the focus on its own. Markdown renders from text, never as HTML. |
| `copy`, `styles.css` | Every UI text, and the design tokens (colors, spacing, radius, type) as CSS custom properties on `:root`: the web chat's light values, the pane's own dark values, and Windows high contrast (`forced-colors`). |
| `officeTheme` | Follows the Office theme: dark on a dark Office background, light on a light one, and the system's color scheme when the host has no Office theme. |
| `taskpane` | The entry point in PowerPoint: document identity, the tool runtime, the Gateway and the pane wired together. |
| `scriptedGateway`, `fakePowerPoint` | The scripted backend the tests drive the pane through, and the deck the write tests write to. |
| `presentationSessionFacade` | `session.create` (`client_app: "office-powerpoint"`, on the server's active model from `models.list`), `client.conversations` (the conversations of this document), `client.conversation.messages` (a conversation's history), `client.list` (the user's attached documents), `chat.send` (messages and answers), `chat.interrupt`, `media.persist`. |
| `clientToolOutcome`, `toolResultBudget` | Pure shaping of a write's mutation record and of the result size. |

Approvals are the backend's: a tool call the user's permission rules ask about
pauses on the backend before the pane is ever called, and the pane only shows
the backend's approval card and sends the user's choice.

## Develop

```
npm ci
npm run dev           # the pane for PowerPoint at https://localhost:3001/taskpane.html
```

`dev` serves the pane over HTTPS with a development certificate
(`office-addin-dev-certs` installs it on first run) and forwards `/ws` to the
Gateway's web channel (`JIUWENSWARM_WEB_CHANNEL_URL`, by default
`ws://127.0.0.1:19000`), and `/file-api/download` to the web frontend's server
(`JIUWENSWARM_FILE_API_URL`, by default `http://127.0.0.1:5173`). Sideload `manifest.powerpoint.xml` into PowerPoint to
open it.

## Checks

```
npm ci
npm run typecheck
npm test
npm run build
```
