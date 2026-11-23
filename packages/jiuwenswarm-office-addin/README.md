# JiuwenSwarm Office add-in

The task pane for PowerPoint, Word, Excel, WPS Presentation, WPS Spreadsheets and WPS Writer: it attaches the open document
to a JiuwenSwarm conversation and runs the conversation's tools in the
document. Each host is a client app of the backend (`office-powerpoint`,
`office-word`, `office-excel`, `wps-presentation`, `wps-spreadsheet`, `wps-writer`, in `jiuwenswarm/office/`); the `tools.json`
and `profile.json` of an app's package directory are the tool contract both
halves read (`@office/<app>/`).

One build serves the three hosts: a page and a manifest per host
(`taskpane.<host>.html`, `manifest.<host>.xml`), each page an entry
(`src/taskpane.<host>.ts`) that gives the shared pane (`startPane`) its app
and its tool runtime. An app is a platform (`office` or `wps`)
and a surface (`presentation`, `document`, `workbook`), which the attach
sends as `host_vendor` and `kind`. Word and Excel run their contract's tools
and send the document's state with each message (`chat.send` `client_state`).

## Code in Word and Excel

`execute_office_js` runs the model's code on the pane's main thread, inside
`Excel.run`, where the host context lives. The first call locks the realm down
(SES `lockdown`, with unsafe error, console and locale taming and severe
override taming, so the pane's own code keeps working); the code runs in a
`Compartment` whose globals are the `Excel` enums and `attachImage` (one PNG
per call), and it reaches the workbook only through `context`, behind a proxy.
The proxy hides Office.js internals, classes and prototypes, refuses event
handlers, and screens every write before it is queued: formulas that reach the
network, a program or another workbook, links out of the workbook, and
refreshes from outside it are refused (`ToolExecutionBlocked`), even when the
code catches the refusal; `fetch`, `XMLHttpRequest`, `WebSocket` and
`EventSource` are refused too. Code that returns with writes still queued
fails, and those writes do not land. Writes whose `context.sync()` failed
count as partly applied, even when the code caught the error, and a sync the
code did not await is waited for before the answer. A `context.sync()` past 30 s or a call
past 90 s is answered at once as uncertain: main-thread code cannot be
stopped, so it may still finish, and the answer says to read back first;
later writes wait in the write queue until it does. Formulas are screened in
English only, so writes to `formulasLocal` are refused. The
call is approved as a whole by the backend's rules before it runs; the pane
cannot ask in the middle of a run.

Word binds the same runner to `Word.run` and the `Word` enums. Its screen
refuses changes to what every document on the computer shares (the templates
with Normal.dotm, building blocks, the list gallery, the master list of
sources), opening or creating other documents, and fields or inserted markup
that make Word fetch from another file, program or address. The runner reads
the Track Changes mode before and after the code and adds
`tracked_change_warning` when the code changed it.

## Word suggestion cards

`propose_doc_edits` checks each edit's `old_text` against the document and
returns at once: the edits it finds become cards under the call in the
transcript, the others come back in `invalid_edits`. A card shows its section,
summary, the change word by word and the alternatives to pick; the user
applies it as an edit (Track Changes off for it), as a tracked change (on for
it) or as a comment, skips it, or undoes it once applied (a hidden bookmark over
the changed words is how the undo finds them; a tracked change is rejected, a
comment deleted; words edited since, or tracked changes already decided in
Word, are left as they are). The user's own Track Changes setting is put back
after each. With more than one card of a call pending, "全部采用" applies them
one after another the way the user last applied a card as a change (an edit
until then), a comment card as its comment; a card that fails says why on
itself.
The cards' state is the pane's alone, kept in the document's settings
(`jiuwenswarm.doc_edits`) by call and edit id, so a reopened pane shows them
again from the conversation's history. The next message of the conversation
that proposed them carries each new decision once it is sent
(`<user_reviewed_edits>`); `withdraw_doc_edits` greys out the conversation's
pending cards, but not one the user is applying.

So far the pane chats, switches between the document's conversations, answers
the backend's approvals and questions, shows each turn's tool calls on a
timeline, runs the read-only PowerPoint tools, and builds and edits slides as
native PowerPoint objects (`presentation_apply_patch`), with an undo icon
under the reply of each turn that wrote. It also runs the tools that work on
a slide's XML: reading and replacing a shape's paragraphs, find and replace,
duplicating and importing slides, copying a picture, building native charts,
the model's own code on a slide's XML or on the slide master (in a sandbox
worker), and the connectors of a patch.

## Support

The PowerPoint manifest requires PowerPoint JavaScript API 1.8, so the add-in
loads only in Microsoft 365 PowerPoint (Windows, Mac and the web). PowerPoint
2016, 2019, 2021 and LTSC (perpetual licenses) and PowerPoint on iPad are not
supported. The Word manifest requires WordApi 1.6, and the Excel one ExcelApi
1.13. The pane declares the
tools the running host can run: a tool's `host_requirements` in `tools.json`
name, for each platform it runs on, the requirement sets it needs (`sets`,
each at its version) and whether it needs a Worker. On PowerPoint 1.8, `presentation_apply_patch` refuses the
row rules, row heights and column widths of `update_table`, which need 1.9.

## WPS

Each WPS app loads the add-in's shell from its own folder (`public/wps/wpp/`
for Presentation, `et/` for Spreadsheets, `wps/` for Writer): the folder's
`index.html` loads the shared `public/wps/shell.js`, and its `main.js` names
the app's documents and pane page. The ribbon button writes the document's GUID
into its custom document properties and opens a pane of its own for that
document (`taskpane.wps-<app>.html?doc=<GUID>`); the pane finds its document by
that GUID (`documentWithId`) and starts through `startWpsPane`. A copy of a
file carries the file's GUID: of the open documents that share one, the first
opened keeps it and the shell gives the others new ones.

### WPS Presentation

The pane runs the PowerPoint tools unchanged over `wpsPowerPointRun`, which
gives them the part of the PowerPoint object model they use on top of WPS's
VBA-style one: a slide's package comes from a saved copy of the deck
(`SaveCopyAs`) and goes back with `Slides.InsertFromFile`, a picture of a slide
from `Slide.Export`, both through files in WPS's temporary directory. WPS keeps
the deck's own theme on every insert, so `edit_slide_master` does not run on
WPS (its contract entry names only `office`), `import_slides` refuses
`keep_source_formatting`, and `edit_slide_xml` refuses code that changes a
theme, master or layout part. A slide written back through its package
(`find_and_replace`, `edit_slide_xml` and the other slide XML tools) loses its
links to other slides: the package holds no other slide for a link to land on,
so the copy goes in without them. Layout ids are positions (`m<i>l<j>`), so
they change when a design or layout is added or removed. WPS keeps its own undo:
the pane offers no undo of a turn there and keeps no turn, though a write that
fails part way is still put back. A dev server
for WPS runs over HTTP: `JIUWENSWARM_PANE_HTTP=1 npm run dev`.

### WPS Spreadsheets

The pane runs the Excel tools unchanged over `wpsExcelRun`, which gives them
the part of the Excel object model they use on top of WPS's VBA-style one.
Notes are WPS's comments; a sheet's id is its name; a column width in points
is turned into WPS's characters by the first column's ratio. A copy goes
through `Range.Copy` for `all`, through plain writes of the values or of the
R1C1 formulas for `values` and `formulas`, and for `formats` through a `Copy`
that puts the destination's contents back, never through the clipboard. WPS
raises no change events with the values before and after, so the messages
carry no `<user_changes>`. WPS runs no Office.js: in place of
`execute_office_js` the pane runs `execute_wps_js`, the model's code on the
workbook's object model in the same locked-down runner. The code gets
`workbook` and WPS's `xl…` constants; each write lands as it is made, so the
runner screens it before it happens. The application and the way back to it
(`Application`, `Parent`), files, links, queries and saving are out of reach
(`WPS_SHEET_UNREACHABLE`).

### WPS Writer

The pane runs the Word tools unchanged over `wpsWordRun`, which gives them the
part of the Word object model they use on top of WPS's VBA-style one. WPS gives
paragraphs, comments and lists no ids: a paragraph's id is where it starts, a
comment's a hash of its author, date and text. The suggestion cards keep their
state in the document's variables, which are saved with it. `verify_doc_visual`
draws the pages of a PDF that `ExportAsFixedFormat` writes to a temporary file.
WPS raises no paragraph change events, so the messages carry no
`<user_changes>`. In place of `execute_office_js` the pane runs
`execute_wps_js` on the document's object model, as in Spreadsheets: the code
gets `document` and WPS's `wd…` constants; the application, the shared
templates, files, hyperlinks, saving and printing are out of reach
(`WPS_WRITER_UNREACHABLE`), and fields or inserted markup that fetch from
outside the document are refused before they land.

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

A tool that edits a slide's XML exports the slide, edits the package, inserts
the edited copy after the slide and deletes the slide: the copy has a new
slide id and the same shape ids. The export it took first is the undo's
snapshot, and in the undo the copy stands where the slide stood, so an undo
puts the slide back once and deletes the copy. Such a write the host fails
part way is rolled back the same way. A patch with connectors does the same
for each slide it draws them on, after the native objects.

`edit_slide_master` clears the undo: the turn that edits the master and the
turns before it cannot be undone. Its slides moved to a new master, and
putting back exports from before would bring the old master back beside it.

Known limits: the undo lives in the pane's memory, so reloading the pane or
reconnecting (which replays the transcript) loses it; the pane keeps the last
10 turns or 64 MB of exports, whichever is less; changes to masters, the theme
and notes are not undone; a change the user makes to a slide between a slide
XML tool's export and its replacement is lost, as those tools take no
fingerprint. A slide XML tool replaces the whole slide, and the copy has a new
slide id and new parts: links from other slides to it, custom shows and zoom
references to it, and comments on it may not survive. Whether a slide's notes
survive the insert is not verified on the host yet. An edit of the slide master
drops the master's and layouts' guides, and cannot be undone. The other slides
take the new master's layouts again, which gives a slide back the layout
placeholders it had deleted; the pane deletes what the relayout added. On
PowerPoint for Mac the old master went both times it was tried;
`old_master_retained` in the result says whether it went. A connector is drawn
where its anchors are when the patch runs but is not glued to them: moving a
shape later leaves the line where it was, and a route through waypoints is
separate straight segments.

## Modules

| Module | Owns |
|---|---|
| `gatewayClient` | The WebSocket to the Gateway: requests, events, reconnects. A refusal rejects with its `code`. It connects as the user named by the pane URL's `u` (the desktop install gives every host the same one), else as a user it keeps per app in `localStorage` (a development sideload). |
| `clientAttachment` | `client.attach` / `client.renew` of one document to one conversation, and the client tool calls of that conversation: each runs once, and the finished ones are answered in one `chat.send` (`source: "client_tool_interrupt"`) while the turn is not running. An answer goes only to the conversation that asked for it. A failed attach tries again; a window another window displaced answers its calls as run there. The renews run on `workerTimer`. |
| `workerTimer` | A timer that keeps time while the pane is hidden: the wait runs in a dedicated Worker (`timerWorker`), as a hidden pane's own timers are throttled and could let the lease lapse. |
| `documentIdentity` | The document's GUID (`jiuwenswarm.doc_id` in the document settings), which is the attach `resource.key`, and its display name. |
| `appContract` | An app (platform, surface, `client_app`, contract) and its `ToolContract`: the attach declaration, and `checkInput`, a call's input against its tool's `input_schema` (the JSON Schema keywords the contract uses; a keyword it does not check fails the call, so the contract tests catch it), with the path of the first problem. Its `supported_tools` are the tools in the contract, implemented by the injected tool runtime, and supported by the running host. |
| `powerPointApp` | PowerPoint's app: its contract, and the names of its tools. |
| `clientToolDispatcher` | Runs a call in the injected tool runtime, writes through `hostWriteQueue`, and shapes the answer: text (and a PNG for renders) for the model, latency telemetry in `data`. A call the host has not finished in 280 s is answered with status `timeout`; a write keeps the queue until the host settles it, and a write that timed out while queued never runs. A host result that does not serialize is answered as an error. |
| `powerPointReadTools` | The read-only tools on `PowerPoint.run`: `read_presentation_context`, `list_slide_shapes`, `read_slide_image` (960 wide by default), `verify_slides` and `verify_slide_visual`. Each call reads in a fixed number of batched syncs, however large the deck; when a shape's text cannot be read, the text is read one sync per shape. On a templated deck the theme palette, theme fonts and template body size come from the package of the first slide. |
| `powerPointWriteTools` | `presentation_apply_patch` on `PowerPoint.run`: checks `base_structure_fingerprint` against the deck, fetches the patch's images, draws it, and answers with `structure_fingerprint_after`; a patch the host fails part way it rolls back (`rolled_back`). It keeps the design system the deck's last patch declared. Also the deck as the undo works it (export, insert, remove, order), and the slide round trip the writes share: export a slide, put an edited package in its place, tell the journal. |
| `powerPointPackageTools` | The tools on a slide's XML: `read_slide_text`, `edit_slide_text`, `find_and_replace`, `duplicate_slide`, `import_slides`, `copy_image_between_slides`, `read_chart_style`, `create_slide_chart`, `edit_built_chart`, `edit_slide_xml`, `edit_slide_chart` and `edit_slide_master`. A write exports the slide, puts the edited copy in its place inside one `PowerPoint.run`, tells the turn's undo (`replaced`), fits again the frames of shapes whose text changed, and answers with `structure_fingerprint_after`; the mutation record maps each slide to its copy. The model's code runs in `ooxmlSandbox.worker` for 90 s at most; what it marked dirty goes in only as one slide with no new macros, controls, embedded objects or links outside the deck other than hyperlinks. A master edit comes in as a new master with the first slide, the other slides of the old master take its layouts by name, the old master goes once no slide uses it, and the undo is cleared. |
| `ooxmlSandbox.worker` | The sandbox: a one-shot module Worker that runs SES `lockdown` and runs the code in a `Compartment` whose only globals are the package as a JSZip-like `zip`, `markDirty`, `escapeXml`, `DOMParser`, `XMLSerializer`, `buildChart` and `insertChart` (create_slide_chart's own insert). No network, no Office.js, nothing of the pane. |
| `presentationPackage`, `presentationChart` | Pure functions over the single-slide package: paragraphs, find and replace, picture copies, chart parts and their style, the theme facts, connectors as DrawingML `cxnSp` (flipped rather than negative, the arrowhead a `tailEnd`), the check of a package code edited, and a master edit made ready to come in as a master of its own. A package that inflates past its limits or names an unsafe part is refused. For `import_slides`, the slide ids of an uploaded deck, refused when a chosen slide, or a layout or master it uses, links a file outside the deck other than by a hyperlink, or holds an OLE object, an ActiveX control or an embedded file. The chart builder draws a checked spec in the house chart standard. |
| `presentationScenePatch` | Reads a patch (roles with their font floors, design-system tokens, exact fields) and draws it in one batch: every slide, shape, table and layout it names is checked before the first change; a failure after that says which slides landed. It tells the turn's undo what it is about to change before it changes it. A connector's route comes from its anchors (on shapes the patch adds, by name, or the slide has, by id, where the patch leaves them) or its own box, checked before the first change; an elbow without waypoints bends at right angles. |
| `presentationAssetClient` | Fetches a published file a call inserts from its signed `/file-api/download` link and takes it only if its size and SHA-256 are the descriptor's. |
| `slideSnapshots` | The per-turn snapshots and the undo (see above). |
| `hostWriteQueue` | The one critical section for document writes, first come first served: tool writes, the end-of-turn hashes and undos, the suggestion cards. It says whether a write ran just now, so Word leaves the pane's own edits out of the user's. |
| `paneController` | The pane's state and the user's actions: opening the document's latest conversation (or a new one), sending (on the model picked under the message box, `chat.send` `model_name`), stopping, answering a question, switching and starting conversations, taking the document back from another window. It also holds the connection state (down, with the next attempt and when) and the outcome of an action the transcript has no place for, until dismissed. The shown conversation's history loads first, again on every attach (as after a reconnect); its live events wait for it. A turn that wrote closes its snapshots and offers its undo while no turn runs; after an undo the next message sent starts with a line saying which slides came back, which the transcript does not show. A `models.updated` reads the models again. |
| `conversationModel` | A pure reducer from a conversation's events to what the pane shows: the transcript, whether the turn runs, and the question it waits on (`chat.ask_user_question` with source `permission_interrupt`, `confirm_interrupt` or `ask_user_interrupt`, answered through `chat.send` as the web chat answers it). A tool call keeps what the model said it is for (`call_goal`) and its arguments. A turn that ran a writing tool ends with an entry naming the calls its undo takes back. The same reducer replays the history. |
| `crossDocumentMessages` | Reads the tasks this conversation sends to other documents (`session_send_message`) and the ones other documents send here (`cross_session_agent`, `session.message.updated`). |
| `view/*` | The DOM, as small components put together by `paneView`: `header` (icons only: history, new conversation, More with the transcript download; the top right corner stays free for the host's menu), `transcript` (messages; under a finished turn's reply, icons to copy it and, while offered, to undo it; dots while the agent works and nothing moves; and `activity`: each run of tool calls as a vertical timeline with live seconds, folded once done to one quiet line, the step's name or "查看过程", with no counts or failures, arguments shown only on request, one at a time), `tray` (above the composer: reconnect countdown, connecting, displaced with "continue here", failed with "retry", what a call waits for the user to do in the document, the outcome of the last action), the selection line (what the user has selected in the document, above the composer), `interaction` (the approval sheet pinned at the bottom: Esc rejects, Enter allows once but not within 350 ms of the sheet appearing; the agent's question card), `composer` (one row, Send and Stop share one button inside it, the model picker below; an Enter that picks an input method candidate does not send). A separate live region reads out replies, approvals and tray lines; the transcript is not one. No component moves the focus on its own. Markdown renders from text, never as HTML. |
| `copy`, `styles.css` | Every UI text, and the design tokens (colors, spacing, radius, type) as CSS custom properties on `:root`: the web chat's light values, the pane's own dark values, and Windows high contrast (`forced-colors`). |
| `officeTheme` | Follows the Office theme: dark on a dark Office background, light on a light one, and the system's color scheme when the host has no Office theme. |
| `startPane`, `taskpane.<host>` | The pane in a host: document identity, the host's tool runtime, the Gateway and the pane wired together. Each entry names its app and builds its runtime; PowerPoint's also has the undo. |
| `scriptedGateway`, `fakePowerPoint`, `fakeExcel`, `fakeWord`, `nodeSandbox` | The scripted backend the tests drive the pane through; a workbook for the Excel tests; a document for the Word tests (ranges as spans of the body's text that move as edits land); the deck the write tests write to, which exports a slide as a real single-slide package, inserts packages keeping their shape ids, and brings in and drops masters; and the sandbox worker bundled with esbuild into a node worker thread. |
| `sessionFacade` | `session.create` (the app's `client_app`, on the server's active model from `models.list`), `client.conversations` (the conversations of this document), `client.conversation.messages` (a conversation's history), `client.list` (the user's attached documents), `chat.send` (messages and answers), `chat.interrupt`, `media.persist`. |
| `clientToolOutcome`, `toolResultBudget` | Pure shaping of a write's mutation record and of the result size. |
| `excelApp`, `excelTools` | Excel's app (its answers run to 40,000 characters, as a sheet's cells need) and its tools on `Excel.run`: `get_cell_ranges` (values, formulas and notes keyed by cell, formatting by the blocks that share it, merged areas, at most `cellLimit` non-empty cells cut at a row with `nextRanges`), `get_range_as_csv`, `set_cell_range` (refuses to overwrite unless allowed, returns each formula's value), `copy_paste_range` (refuses to cut a merged area or overwrite), `execute_office_js` and `read_surface_context`. Notes need ExcelApi 1.18. |
| `selectionLine`, `hostLine` | What the user has selected, as one line above the composer: the cells in Excel, the slides and shapes in PowerPoint and WPS, how many characters (white space aside) in Word. Read again 300 ms after the host's selection event stops (`DocumentSelectionChanged`; WPS's `WindowSelectionChange`, which WPS does not raise for text selected inside a shape); a failed read keeps the line. `hostLine` is a line of the host's own the pane shows: this one and the cell-edit wait. |
| `excelCellEdit` | Excel takes no request while the user types in a cell. Before each tool call the pane asks Excel for one property; when a cell is being edited, the tray asks the user to press Enter or Esc and the call waits on a request run with `delayForCellEdit` (30 s, 8 s on the web). Past that, or where Excel does not hold the request, the call fails with `EXCEL_CELL_EDIT_MODE`, telling the model to ask the user. |
| `excelGuard` | What Excel refuses to write whatever the approval: the formula functions that reach outside the workbook, links to other workbooks or files, and, for code, hyperlinks, inserted workbooks and outside refreshes. |
| `excelState` | The workbook's state each message carries: `<initial_state>` (sheets with ids and sizes, Excel tables and their columns) with a conversation's first message from this pane, `<user_changes>` (cell edits, sheets added, deleted and renamed, heard from the host's events from ExcelApi 1.14, renames from 1.17; this add-in's own edits left out), and `<user_context>` (active sheet, selected chart or ranges, names cleaned for display). Changes count as told only once `chat.send` took the message. |
| `officeCodeRunner` | The runner of `execute_office_js` (see Code in Word and Excel above), host-neutral: each host gives its `run`, enums and write screen; `wpsCodeRuntime` runs `execute_wps_js` on a WPS document. |
| `wpsDocument`, `wpsPane`, `wpsInput` | A WPS pane's document (`documentWithId`, by the GUID the shell wrote, in `Presentations`, `Workbooks` or `Documents`), its start (`startWpsPane`), and the paste WPS also sends the document while a pane field has the focus. |
| `wpsPowerPoint`, `wpsExcel`, `wpsWriter` | The PowerPoint, Excel and Word object models the tools use, over WPS's (see WPS above). |
| `wpsFiles` | Temporary files in WPS's temporary directory, through which an adapter hands a document to WPS and back (`withFile`, `written`). |
| `wordApp`, `wordTools` | Word's app (its answers run to 200,000 characters, as the industry add-in allows) and its tools on `Word.run`: `read_doc_section` (by heading or paragraph range, at most 400 paragraphs a call, each with its index and markers, optionally its formatting), `search_doc_text` (each hit's paragraph index and a snippet), `edit_doc_text`, `edit_doc_list` (a new bulleted or numbered list by `startNewList`, as list style names are in Word's display language, or one item joining a list), `collapse_blank_paragraphs` (empty means only spaces and tabs, so page and line breaks stay; keeps empty paragraphs that hold a picture, shape, comment, tracked change, field or content control, list items and table cells), `verify_doc` (styles, list numbers, cells per table row), `execute_office_js`, `read_surface_context` (a note that the state was added at the turn's start), the card tools, and the capture `explain_edits`' reviewer checks claims against (final text and pending tracked changes). |
| `wordText`, `wordPassage` | The text rules of the edit tools: quotes match either kind, a passage past Word's 255-character search limit is found by its first and last 255 characters, a text of several lines is that many whole paragraphs, and an edit replaces only the words that differ (a word-level diff), never across a field, note or picture. |
| `wordParagraphs` | Paragraphs as the model reads them: final text after markers (`[Heading2]`, `[1.1.]`, `[tbl:1]`, `[page-break]`, `[has: …]`, `[img]`, `[field×2]`, `[fn]`, `[en]`). |
| `wordGuard` | What Word code may not do whatever the approval (see above). |
| `wordState` | The document's state each message carries: `<doc_state>` (the full text under 5,000 words and 30,000 characters, else a 500-character opening; the outline with list numbers and fonts, body font, `Language:` line, custom styles, pictures, Track Changes, columns on desktop, headers and footers, comment threads, content controls and tracked changes, the last three inside `<untrusted_content>`), `<user_selection>`, `<user_changes>` (paragraphs added, changed or deleted, from WordApi 1.6 paragraph events, leaving out those within a second of the pane's own writes) and `<user_reviewed_edits>`. |
| `docEditReview`, `view/editCards` | The suggestion cards' state and actions, and their view (see Word suggestion cards above). |

Approvals are the backend's: a tool call the user's permission rules ask about
pauses on the backend before the pane is ever called, and the pane only shows
the backend's approval card and sends the user's choice.

## Develop

```
npm ci
npm run dev           # the panes at https://localhost:3001/taskpane.<host>.html
```

`dev` serves the pane over HTTPS with a development certificate
(`office-addin-dev-certs` installs it on first run) and forwards `/ws` to the
Gateway's web channel (`JIUWENSWARM_WEB_CHANNEL_URL`, by default
`ws://127.0.0.1:19000`), and `/file-api/download` to the web frontend's server
(`JIUWENSWARM_FILE_API_URL`, by default `http://127.0.0.1:5173`). Sideload `manifest.<host>.xml` into the host to
open its pane.

`npm run build:desktop` builds the three pages under `/office/` into the web
frontend's `dist/office/`, each with its `manifest.<host>.xml` beside it; the
desktop serves them from there and installs the three panes
(`jiuwenswarm/office/README.md`, 桌面安装).

## Checks

```
npm ci
npm run typecheck
npm test
npm run build
```
