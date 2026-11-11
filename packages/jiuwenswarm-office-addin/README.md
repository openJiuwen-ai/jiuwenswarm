# JiuwenSwarm Office add-in

The PowerPoint task pane: it attaches the open presentation to a JiuwenSwarm
conversation and runs the conversation's PowerPoint tools in the document.
The backend half is the `office-powerpoint` client app
(`jiuwenswarm/office/`); its `powerpoint/tools.json` and `profile.json` are
the tool contract both halves read (`@contract`).

So far the pane chats, switches between the document's conversations, answers
the backend's approvals and questions, shows each turn's tool calls on a
timeline, and runs the read-only PowerPoint tools. The write tools follow in
later changes.

## Support

The pane needs PowerPoint with PowerPoint JavaScript API 1.8 for the slide
images and the visual review, so it fully works only in Microsoft 365
PowerPoint (Windows, Mac and the web). PowerPoint 2016, 2019, 2021 and LTSC
(perpetual licenses) and PowerPoint on iPad are not supported. On a host with
an older API set the pane still attaches, and declares only the tools that
host can run (`host_requirements` in `tools.json`): `read_presentation_context`
needs 1.2, `list_slide_shapes` and `verify_slides` need 1.4.

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
| `hostWriteQueue` | The one critical section for document writes, first come first served. |
| `paneController` | The pane's state and the user's actions: opening the document's latest conversation (or a new one), sending (on the model picked in the More menu, `chat.send` `model_name`), stopping, answering a question, switching and starting conversations, taking the document back from another window. It also holds the connection state (down, with the next attempt and when) and the outcome of an action the transcript has no place for, until dismissed. The shown conversation's history loads first, again on every attach (as after a reconnect); its live events wait for it. |
| `conversationModel` | A pure reducer from a conversation's events to what the pane shows: the transcript, whether the turn runs, and the question it waits on (`chat.ask_user_question` with source `permission_interrupt`, `confirm_interrupt` or `ask_user_interrupt`, answered through `chat.send` as the web chat answers it). A tool call keeps what the model said it is for (`call_goal`), its arguments and its outcome: the visual review's verdict, or a call the backend answered for a pane that had gone (`client_gone`). A turn that ran a writing tool ends with a "what this turn changed" entry. The same reducer replays the history. |
| `crossDocumentMessages` | Reads the tasks this conversation sends to other documents (`session_send_message`) and the ones other documents send here (`cross_session_agent`, `session.message.updated`). |
| `view/*` | The DOM, as small components put together by `paneView`: `header` (icons only, history, new conversation, More with the model; the top right corner stays free for the host's menu), `transcript` (messages, and `activity`: each run of tool calls as a vertical timeline with live seconds, folded to one summary line once done, arguments shown only on request, one at a time), `tray` (above the composer: reconnect countdown, connecting, displaced with "continue here", failed with "retry", the outcome of the last action), `interaction` (the approval sheet pinned at the bottom: Esc rejects, Enter allows once but not within 350 ms of the sheet appearing; the agent's question card), `composer` (Send and Stop share one button). A separate live region reads out replies, approvals and tray lines; the transcript is not one. No component moves the focus on its own. Markdown renders from text, never as HTML. |
| `copy`, `styles.css` | Every UI text, and the design tokens (colors, spacing, radius, type) as CSS custom properties on `:root`: the web chat's light values, the pane's own dark values, and Windows high contrast (`forced-colors`). |
| `officeTheme` | Follows the Office theme: dark on a dark Office background, light on a light one, and the system's color scheme when the host has no Office theme. |
| `taskpane` | The entry point in PowerPoint: document identity, the tool runtime, the Gateway and the pane wired together. |
| `scriptedGateway` | The scripted backend the tests drive the pane through. |
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
`ws://127.0.0.1:19000`). Sideload `manifest.powerpoint.xml` into PowerPoint to
open it.

## Checks

```
npm ci
npm run typecheck
npm test
npm run build
```
