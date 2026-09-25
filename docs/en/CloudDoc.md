# Cloud-document co-editing (Co-scribe)

## 1. Overview

Co-scribe makes an agent a collaborator inside a shared cloud document. The deployment owner holds the platform credentials and is the only person who can grant anything; collaborators reach the agent by @-mentioning it in a comment, and the agent edits **only inside the passage the comment marked**, only while the document's watch is on, and leaves a receipt for every write.

```
You      (select a sentence, add a comment)   @docs-agent please tighten this
Agent    ⏳ Working on it…  →  ⏳ Reading the document…  →  ⏳ Checking the comment's range and writing…
Agent    Edited directly; the changes are highlighted in yellow.
         If this is not what you wanted, @-mention me with what to change;
         to undo it, use the platform's version history.
```

Three surfaces share one machinery:

| Surface | Who is present | What it can do |
|---|---|---|
| **A comment** in the document | nobody -- the turn is unattended | a bounded edit inside the marked passage, or an answer in the thread |
| **Jiuwen chat** | you | read, list, reply, edit, write regions, add pages, create/share/trash -- every write asks first |
| **The Docs panel** and the document workbench | the deployment owner | register documents, turn each document's watch on or off, read the record of grants and writes |

Two platforms, Google and Feishu, in **three formats: documents, spreadsheets and slide decks**. Everything platform-specific sits behind one provider interface.

Markdown files were served once and were withdrawn (2026-09-08): a comment on a `.md` carries neither an anchor nor the quoted text every other format's edit window is computed from, so an unattended turn on one had nothing to bound it. A document adopted while the format was served is retired at startup; nothing on the platform is touched.

**Disabled by default.** With `clouddoc.enabled: false` the watcher is never constructed. The switch is on the **Application plugins** page (the Co-scribe card); the Docs entry appears in the sidebar only while it is on.

## 2. Setting up

### 2.1 An identity for the agent

The agent needs an identity of its own -- not yours -- so its edits are distinguishable from yours in the document's history and its access can be revoked without touching your account.

**Google.** In the [Cloud console](https://console.cloud.google.com/) create a project, enable the **Docs, Drive, Sheets and Slides APIs**, create a service account and download a **JSON key**. The account's address (`name@project-id.iam.gserviceaccount.com`) is the agent's identity in every document. Consumer Google gives service accounts no Drive storage, so the agent can edit documents shared with it but cannot own new ones -- create the document yourself and share it.

**Feishu / Lark.** Co-scribe drives the platform through the official `lark-cli`, always `--as bot`. Register the app once with `lark-cli config init --app-id … --app-secret-stdin` (the secret lives in the CLI's own store, never on a command line), then write a small credentials file the deployment points at:

```json
{"app_id": "cli_xxx", "app_secret": "…", "bot_open_id": "ou_xxx", "profile": "cli_xxx"}
```

`bot_open_id` is how the agent recognises its own comments; the tenant's `whoami` does not always carry it. Keep both kinds of key file outside the repository -- anyone holding one can act as the agent.

### 2.2 Share the document

Share the document with the agent's address with **Editor** access. Commenter access is not enough, and the failure is quiet: Google stops returning a revision id, the permission listing returns 403, and comments still read -- so the agent would appear to work and never be able to write. Such a document is refused at admission and shown as *comment-only* in the panel, with the fix.

On Feishu, add the app as a collaborator of the document (or of the wiki space that holds it). To let the panel **discover** documents by itself, make the app a member of the team wiki space: discovery walks the spaces the managed documents live in, and a space the app is not a member of answers `131006`; the panel then says which space to add it to.

### 2.3 Connect

Either write the key's path into `config.yaml`:

```yaml
clouddoc:
  enabled: true
  connections:
    - credentials_file: /secure/path/to/key.json
      documents: []            # links or bare ids; the panel fills this in
```

or open the **Docs** panel and add the connection there -- paste the key's path, paste the JSON, or choose the file. An uploaded key is stored under `<workspace>/config/clouddoc-keys/` (0600) and only its path enters the config. One connection is one platform account; **a document belongs to exactly one connection**. Adds and removals from the panel take effect at once and are written back to `config.yaml`.

### 2.4 Register documents

Sharing is what puts a document under management. Opening the panel, pressing **Refresh**, and a background pass every five minutes (`auto_discover_shared`) adopt every document the account can edit -- on Google, everything shared with it; on Feishu, everything in the wiki spaces the managed documents live in. A document the listing cannot reach (a Feishu file outside those spaces, a document the app just created) is adopted by pasting its link into the box at the foot of the table and pressing **Adopt** (or Enter): the link is verified first, a document that passes is adopted, and one that cannot be is told why on the spot; the result says whether its watch is off or on.

Both roads end in the same place, under the same watch policy:

- **Adoption** means the document is on this connection's polling list and in `config.yaml`: every poll reads its comments from then on, and it is still there after a restart. Discovery only adds; a document it cannot list is not un-adopted for that reason, and only a per-document probe that the platform answers with "gone" or "no longer shared" retires one. So a Feishu document adopted by link keeps being polled even if it never sits in any wiki space, and a mention on it is seen.
- **The watch (write authority) is off by default.** `auto_watch_on_adopt` defaults to `off`: whether a document was discovered or pasted, after adoption the agent reads and replies, and a mention does not edit. To let it edit, turn the watch on in the row -- that issues a dated grant, recorded in the registry and the audit journal -- or set `auto_watch_on_adopt` to `apply_scoped`, in which case both roads issue at adoption and the panel says so in the adoption result.

**Adoption grants nothing.** A newly adopted document is polled and nothing more (`auto_watch_on_adopt: "off"`): a collaborator who @-mentions the agent there is told, once, that the watch is off and who can turn it on.

### 2.5 Turn the watch on

The **watch** column is the switch. Off means no turn is ever dispatched for that document; on means a mention gets a bounded edit. Turning it on asks for a second click (it is a standing delegation); turning it off takes effect at once. A ticked selection can be turned on or off together. A watch issued without a term **expires after 30 days**; the document's record offers renewal or *permanent*, and an expired or revoked watch stays as a tombstone so nothing re-issues it silently.

## 3. Working from a comment

1. Select the passage you want changed -- the selection is the boundary, not just context.
2. Add a comment and **@-mention the agent's address** (the comment itself, or a reply under it). A mention is the only summons on both platforms; Google's "assign to" field is not read.
3. Say what you want. Within seconds (the poll interval, 5–30 s) the agent posts a placeholder, narrates its steps into it, and either edits or answers.

What the agent may write is computed in code from the selection: `exact` is the selection itself; the commenter's own words widen it to the enclosing **sentence**, **line** or **paragraph** (「这句」「这行」「这段」), never further, and the tier is disclosed in the reply and recorded in the receipt. An edit that reaches outside the window is refused whole. On Google the change is **highlighted in yellow** and the receipt offers to clear it; Feishu's write channel carries no highlight, so the reply lists each change instead.

A **follow-up** needs another mention: replies without one are read as thread context but do not wake the agent. A question gets an answer, not an edit. Two comments on the same passage that contradict each other are not arbitrated -- the agent says so and asks you to coordinate. The agent never resolves a thread; closing it is the reader's acceptance.

**Undo is the platform's version history** (Google "Version history", Feishu 历史版本). The receipt holds the before-text for anyone who wants only that batch back.

Spreadsheets and decks have platform limits worth knowing before you comment -- see §5.

## 4. Working from chat

In chat the agent reaches every registered document across all connections. With several documents managed it must be told which one -- by title, link or id in your own words; it is refused (in code) from choosing one for you, and asked to ask. Every write asks for confirmation first and returns a **receipt id**.

| Tool | What it does |
|---|---|
| `clouddoc_list_documents` | the registered documents, with format and platform |
| `clouddoc_read` | the body as plain text; grids and decks also return every addressed cell/shape and how many sheets or pages exist |
| `clouddoc_list_comments` / `clouddoc_reply_comment` | read and answer threads (never a resolved one) |
| `clouddoc_batch_edit` | several text replacements, one atomic submission; can serve a comment (`for_comment_id`) and is then bounded like the unattended path |
| `clouddoc_write_region` | say what a cell range or a slide shape should read like -- moves, swaps and clearing in one write |
| `clouddoc_add_page` | a new worksheet or slide, answering the address to write through next |
| `clouddoc_create_document` / `share_document` / `trash_document` | lifecycle acts, each with a receipt; sharing addresses must come from your words |
| `clouddoc_workmode_get` / `edit` | the deployment's working-style file |

Rules the tools enforce: **read before you write** (a shared document changes under other hands); the body is plain text on Google and on every grid and deck, so markdown markers are refused; a cell write that would overwrite a formula is refused for the whole batch; a deck's speaker notes are written only when you asked for notes; a chat write is refused while an unattended turn for the same document is outstanding.

**Opening a document** from the panel or from the chip above the composer shows the platform's own editor in a workbench tab, with the session's chat docked below it and the document's receipts and watch beside it. Each session keeps its own tabs (at most six).

## 5. Formats and platform limits

| | Google | Feishu |
|---|---|---|
| Highlight on write | yes, cleared on resolve or by hand | no -- the reply lists the changes |
| Optimistic lock | documents and decks; **not** spreadsheets | none -- every region write is read back and the receipt is marked *unverified* on drift |
| Several edits in one write | yes | one edit per write |
| Comment anchor on a **document** | quoted text | quoted text (truncated at 128 characters, unmarked) |
| Comment anchor on a **spreadsheet** | single cell: the cell's value; a range: **nothing** | the cell's address is prefixed to the quote (`D2 …`) |
| Comment anchor on a **deck** | the shape clicked, or the quoted text | the quoted text; regions must sit on one slide |
| Add a page | worksheet by name; slide with title + body boxes | worksheet by name; slide needs a title (identical requests are folded into one page) |

**Spreadsheets from a comment.** The cell a comment marks is recoverable only from its quoted text: a value repeated in the sheet cannot be addressed from a comment, a multi-cell selection stores no quote at all on Google, and one comment authorizes exactly one cell. Comment on a cell whose value is unique, or do the change in chat, where cells are addressed by A1.

**Decks.** A comment left on a shape as a whole authorizes that shape; a page that has no text box yet is not writable from a comment -- add a page from chat first.

## 6. Configuration

```yaml
clouddoc:
  enabled: false
  connections: []                 # one entry per platform account (see §2.3)
  poll_interval_seconds: 30       # 5–600; the panel offers 5/10/15/30
  turn_timeout_seconds: 540       # clamped below the transport ceiling
  session_max_turns: 50           # rotate a document's session after this many turns
  auto_discover_shared: true      # the five-minute adoption pass
  discover_interval_seconds: 300
  auto_watch_on_adopt: "off"      # "apply_scoped" issues a watch on adoption (a batch signature)
  model_name: ""                  # the model unattended turns run on; a document may pin its own
  mode: mandate                   # or direct: no receipts, no unattended dispatch
  workmode_file: ""               # the working-style file; default under the config dir
  conventions_marker: "co-scribe 约定"   # also picks the built-in style's language
  agent_roster: []                # other agents' Feishu open_ids, so a mention by an agent never summons one
  dispatch_rate_max: 10           # per document, per rolling window: the loop brake
  dispatch_rate_window_seconds: 120
  rail:
    max_quote_chars: 400          # backstop; the provider's measured limit wins (418 / 128)
    max_insert_chars: 2000
    max_edits: 10
```

Every configurable string -- the conventions marker and each entry of the two word lists -- must not equal or prefix another; this is checked at startup and the feature refuses to start if it fails.

**Permissions.** With `permissions.enabled: true`, the four tools an unattended turn holds must be `allow`, since nobody is there to answer a prompt: `clouddoc_read`, `clouddoc_list_comments`, `clouddoc_apply_for_comment`, `clouddoc_reply_comment`. The chat writes default to `ask`. Under **Full Access** (no confirmation channel) the acts that hand out access or cannot be undone -- create-and-share, share, trash, and any write whose receipt ledger is unavailable -- are refused rather than performed silently; the panel shows a banner saying so.

**Modes.** `mandate` is the full machinery. `direct` is the deliberate baseline: adoption and direct editing, no receipts, no unattended dispatch; the panel keeps a standing banner while it is on.

## 7. The record

Every write is preceded by a **pending** receipt and followed by **applied** or **aborted**, recorded inside the write primitive where the model can neither skip nor disable it. A crash between the two leaves a pending receipt the startup sweep settles as **unknown** rather than guessing; a region write whose read-back disagrees is **applied_unverified** with what differed.

The Docs panel's **document record** puts three things on one timeline: the grant lineage (issued, renewed, revoked, expired), the dispatches and refusals, and the writes. It is exportable as CSV -- a snapshot of the view, not the record. The watch registry is the current state; the audit journal is append-only and survives revocation and re-granting; the receipt ledger is the writes. All three live under `<workspace>/config/` and are refused to the generic file tools.

## 8. What holds without anyone watching

Guardrails are code, not prompt:

- an unattended turn holds a **closed set of four tools**, with the document and comment bound by the dispatcher -- a document argument from the model is replaced by the bound value;
- the **edit window** is computed from the selection's geometry; a mechanisable instruction ("shorten", "delete X") is checked against the edit before the write; region writes are read back;
- the pre-write checkpoint re-reads the watch: a revocation, expiry or tier change intercepts a write in flight;
- a reply that claims a change the ledger did not record is corrected in the thread;
- nothing a service account or a rostered agent writes is ever a summons, and a per-document **rate brake** stops a pointer loop within one poll;
- shell invocations of the platform CLI are refused unless they are plainly reads, and the machinery's own files are refused to generic file tools.

## 9. Document conventions

A collaborator can set per-document writing conventions by leaving a top-level comment that starts with the conventions marker:

```
co-scribe 约定
Keep sentences short.
Use the full product name on first mention.
```

They bind **style only**; anything in them about what the agent may edit or whose approval counts is ignored, replies never count, and the earliest such comment wins. The deployment's own style lives in the working-style file, editable from chat.

## 10. Limits and troubleshooting

- **Latency**: polling every N seconds means N/2 of average wait plus about six seconds of the turn's own round trips before the placeholder appears; the model's work comes after.
- **Quota**: all watched documents share the account's quota; every managed document costs one comment query per poll.
- **One gateway**: two instances sharing a state file would each dispatch the same trigger.
- **Workspace policy**: some tenants forbid out-of-domain service accounts; use one your own administrator created.

| Symptom | Cause |
|---|---|
| Nothing happens after mentioning the agent | The document's watch is off (the agent says so once, in the thread), the document is not registered, or the mention names another agent's address |
| "The watch is off for this document" | Turn it on in the Docs panel; then @ the agent again |
| The agent replied but did not edit | The comment asked a question; or the edit reached outside the window; or two comments on the passage contradict each other -- the reply says which |
| "the quoted text appears N times in this sheet" | The cell's value is repeated; comment on a unique cell or do it in chat by A1 |
| "The user does not exist" (Feishu 20008) | A worksheet name that is not in the workbook -- check it with `clouddoc_read` or add it with `clouddoc_add_page` |
| A document reads "not shared" after you fixed it | Press **Refresh**; a document that failed repeatedly is re-checked once per start |
| The panel says discovery is unavailable on a Feishu connection | Add the app to the wiki space it names, or adopt by pasting links |
| A document disappeared from the list | It was retired: deleted on the platform or unshared, confirmed by the metadata query before anything was dropped |
