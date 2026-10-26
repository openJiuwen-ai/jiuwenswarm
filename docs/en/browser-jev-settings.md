# Jev Model Settings

Configure Jev once under **Settings > Models > Jev**. Browser decisions and multi-model
verification share the provider, endpoint, credential and model. The original browser template
and storage (`browser.decision` and its credential environment variable) are retained.
Existing browser configuration needs no re-entry.

## Shared Model

Select **Jev Model Configuration > Configure** in this section to edit the provider,
API key and model in one model-style dialog. Only Custom shows an API base URL field.

| Provider | API base | Default model |
| --- | --- | --- |
| OpenRouter | Built in: `https://openrouter.ai/api/alpha` | `typesafe/jev-1.13` |
| TypeSafe | Built in: `https://api.typesafe.ai/v1` | `jev-1.13.0` |
| Custom | Required HTTPS base URL | `jev-1.13.0`, editable |

Saved keys are prefilled, masked by default, and can be revealed. Unconfigured keys are empty.
Cancel discards edits. Selecting a provider loads its template and independent credential;
the selection is applied only when saved. No protocol, context-window or custom-name fields are shown.

Custom services must implement the TypeSafe-compatible Jev `/systemone` API. Enter the base
URL without `/systemone`. This is not a Chat Completions endpoint. OpenRouter uses its native
Jev `/decisions` API.

## Independent Controls

- LLM / Shadow / Hybrid remains under **Browser**.
- Collaboration, JEV decision and verification interval remain under **Experimental**.
- Saving the shared connection does not enable either feature. Collaboration may use Jev
  even when browser decisions remain in LLM-only mode.
- Clearing the active key returns browser decisions to LLM only and disables collaboration's
  JEV decision, without deleting other providers' credentials.
- Enabling Shadow / Hybrid without a key shows an error in the UI language and retains the
  previous mode. Leaving Browser settings clears the error; returning does not restore it.

Both consumers select `/decisions` or `/systemone` from the shared provider. Collaboration
reads the latest settings when its runtime is rebuilt. Browser path, display mode, cookies
and browser lifecycle are unchanged.

## Legacy Configuration

Existing `experimental.multi_model_collaboration.jev` data is retained but no longer read.
It never overwrites browser settings. Users who only configured the independent collaboration
connection must configure the shared connection once. Legacy `moa_jev_api_*` and
`moa_jev_model_name` writes explicitly report the new settings location instead of saving unused data.
