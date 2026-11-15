# providers/feishu

The Feishu / Lark implementation of the provider contract, driven through the
official `lark-cli`, always as the app (`--profile <app_id> --as bot`).

**Purpose.** `cli.py` is the seam: one class that builds, runs and classifies every
command the provider issues, with a timeout, and turns the CLI's JSON errors into
`ProviderError` kinds. `reading.py` holds what is known about a document without
changing it: references and metadata, the app's capabilities on it (asked of the
tenant at runtime, degrading rather than failing), and the body as markdown through the
CLI's own conversion. The comment layer and the
write path build on it in the parts that follow. `formats.py` holds the tables the
provider dispatches Sheets and Slides through; their handlers follow, and until then
those formats are refused rather than read as a docx.

**Public names.** `LarkCli`, `LarkResult`; `FeishuReader`; in `formats`:
`TEXT_DOMAIN` and the readers and writers the provider calls.

**Imports.** `providers.base`, `providers.textmap`, `providers.formats`; nothing
above the providers layer and nothing from the Google package.

**Platform facts the code relies on**, each measured on a tenant and noted at the
site: no document-comment event (the watcher polls), no background colour on the
write channel (replies itemise changes instead of highlighting), one command per edit
(no atomic multi-edit), quoted text truncated at 128 characters without a marker.
