# Context Cockpit

See how much context a conversation uses, what it is made of, and how it changes turn by turn and request by request.

## Features

- **Current context**: observed input tokens, window capacity and occupancy. Missing data stays unknown; estimates are never presented as actual usage.
- **Context composition**: estimates by system prompt, tool definitions, skills and messages, with shares calculated over known categories only.
- **Per-request trend**: input tokens and adjacent deltas for the last 24 model requests, with model, latency, output, cache and the tool calls seen between requests.
- **Agent Loop trace**: status, latency and usage for model steps and tool batches, with redacted argument and result summaries.
- **Completed turns and message browsing**: usage for the last 12 completed turns and the latest 12 conversation messages, up to 600 characters each.

## How it works

- No configuration required. Click the context observer above the composer to open the side panel; later turns update automatically, and Refresh re-reads on demand.
- The plugin subscribes to `context.usage.updated` to refresh occupancy and usage after each turn, and to `turn.step` plus `tool.call` to keep the execution trace current.
- All data comes from public session Ops (usage / model / turns / messages). Observation is read-only and never interferes with model reasoning or tool execution.
- The last 12 completed turns persist locally and survive restarts. The execution trace and per-request trend live only for the current run. Message bodies are never stored.
- Composition is an estimate and unknown values stay unknown. All assets ship with the plugin; no CDN or external service is required.

## Why it helps

- Spot when context is running tight in long conversations and decide when to clear or start a new session.
- See where a slow turn went by following model steps and tool-call latency in the trace.
- Compare input growth across models or agents to judge whether context usage is reasonable.