# Topic recommendations

Turning on **Topic recommendations** in **Settings → General** directly enables a saved recommendation model without opening a dialog. If no model is configured, configure a provider, OpenAI-compatible protocol, API key, and model, then select **Save and enable**. Cancelling or failing to save leaves recommendations off. Use the model card's edit button to reopen configuration. Without an explicitly configured recommendation model, no chat model is inherited and no default model card is shown.

The dialog uses the same connection form as video generation settings: provider, OpenAI-compatible protocol, API key, and model, plus an API URL for custom providers. Model aliases, reasoning levels, and context windows are not required.

The saved model appears below the switch with edit and delete actions. The connection is saved independently and is not added to **Settings → Models**. Turning the switch off preserves it; deleting clears the dedicated connection and any legacy binding and turns recommendations off. Existing chat models are never displayed as dedicated topic models.

When editing, the API key input contains the actual saved key, hidden by default as a password. Use the eye button to reveal or hide it. Ordinary status queries omit the key; only the editor explicitly requests it. Enter and save a new key to replace the current value.

When enabled, the home page shows topic cards once the backend supplies them. The disabled-feature hint stays hidden while content is pending. Only turning recommendations off displays the hint and its link to **Settings → General**.

Topic search requires both DuckDuckGo and Bing free search to be enabled in **Settings → Agent**. If search was not configured at startup, the next recommendation read after saving the search configuration recreates the topic service while preserving existing preparation tasks and the outcome queue.

With prompt suggestions enabled, a generated follow-up question appears as gray placeholder text in the empty composer after an answer, regardless of the timeline's scroll position. Press Tab or Right Arrow to fill it as an editable draft; use Send to submit it. Typing another question or starting a new turn hides the old suggestion. The normal placeholder remains until the backend provides a suggestion. Home-page topics and prompt suggestions have independent switches.

Suggestions appear only in the composer, without a repeated chip below the answer or a "Next:" prefix. Returning to a conversation or reconnecting retrieves unconsumed suggestions for its latest completed answer. Accepted, ignored, and stale suggestions are not restored.
