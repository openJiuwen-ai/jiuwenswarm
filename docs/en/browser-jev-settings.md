# Browser Jev Settings

Open **Settings > Browser > Jev Model Configuration > Configure** to edit the provider,
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

The separate LLM / Shadow / Hybrid mode setting remains unchanged. Saving the model does not
enable Jev automatically. Clearing the active key first returns to LLM mode without deleting
other providers' credentials. Chrome path, display mode, cookies and browser lifecycle are unchanged.
