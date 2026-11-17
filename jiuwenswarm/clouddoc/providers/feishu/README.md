# providers/feishu

The Feishu / Lark implementation of the provider contract, driven through the
official `lark-cli`, always as the app (`--profile <app_id> --as bot`).

**Purpose.** `cli.py` is the seam: one class that builds, runs and classifies every
command the provider issues, with a timeout, and turns the CLI's JSON errors into
`ProviderError` kinds. `reading.py` holds what is known about a document without
changing it: references and metadata, the app's capabilities on it (asked of the
tenant at runtime, degrading rather than failing), and the body as markdown through the
CLI's own conversion. `comments.py` adds comment
threads: every page of cards and replies, authors told apart by id, and the bot's own
comments recognised by its open id. The write path builds on it in the part that
follows. `formats.py` holds the tables the
provider dispatches Sheets and Slides through; their handlers follow, and until then
those formats are refused rather than read as a docx.

**Public names.** `LarkCli`, `LarkResult`; `FeishuReader`, `FeishuCommenter`; in `formats`:
`TEXT_DOMAIN` and the readers and writers the provider calls.

**Imports.** `providers.base`, `providers.textmap`, `providers.formats`; nothing
above the providers layer and nothing from the Google package.

**Platform facts the code relies on**, each measured on a tenant and noted at the
site: no document-comment event (the watcher polls), no background colour on the
write channel (replies itemise changes instead of highlighting), one command per edit
(no atomic multi-edit), quoted text truncated at 128 characters without a marker.

**Comments, as the contract reads them.** A Feishu comment is a thread whose opening
message is reply one; `list_comments` splits it, so `DocComment.content` and its
mentions come from that message and `DocComment.replies` holds only what came after,
as on Google. A spreadsheet comment's quote opens with the cell's A1 address, which is
stripped only when the document's format is known to be a spreadsheet; when the format
lookup fails the quote is kept whole, anchors as text, and a warning is logged.
Another agent cannot be recognised from the comment payload, so a deployment that
shares documents with other agents declares their open_ids in `agent_roster`; an
author outside it is treated as a person.
