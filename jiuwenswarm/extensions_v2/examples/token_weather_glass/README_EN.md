# TokenWeather

Shows live context usage for the current conversation above the input box, making it easy to see how much room remains before the window limit.

## Features

- **Live status bar**: used tokens, window limit and share, plus a trend over the last 12 turns.
- **Weather state**: clear below 25%, cloudy at 25%-50%, rain at 50%-75%, storm at 75%-90%, and near-compaction at 90% or above.
- **Usage details**: click "Open details" to see a per-turn breakdown in the right panel.
- **Visibility switch**: control the status bar under Settings → General → Display.

## How it works

1. The plugin listens to `context.usage.updated` and refreshes on every usage change.
2. Values come from `ops.session.usage()`; only observed fields are shown, unknown values are never invented, and real zeros are preserved.
3. After a page refresh the plugin re-queries the current conversation; history is re-collected after reload or restart.
4. The status bar reuses host theme styles and declares no custom colors, gradients or shadows.

## Notes

- The values reflect observed context usage, not a provider bill.
- Context can drop because of compaction; it does not mean a turn consumed a negative amount.
- Data is isolated per conversation, so switching conversations shows the matching usage.

## Install

From the repository root:

```sh
python jiuwenswarm/extensions_v2/examples/token_weather_glass/pack.py --output /tmp/token-weather-glass.zip
```

Install it from the plugin detail page to activate. Uninstalling does not remove the client's display preference.
