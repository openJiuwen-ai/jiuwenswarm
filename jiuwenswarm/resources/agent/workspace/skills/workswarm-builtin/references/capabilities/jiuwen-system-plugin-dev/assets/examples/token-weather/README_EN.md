# TokenWeather

Shows live context usage for the current conversation above the input box, making it easy to see how much room remains before the window limit.

## Features

- **Live status bar**: used tokens, window limit and share, plus a trend over the last 12 turns.
- **Weather state**: clear below 25%, cloudy at 25%-50%, rain at 50%-75%, storm at 75%-90%, and near-compaction at 90% or above.
- **Usage details**: click "Open details" to see a per-turn breakdown in the right panel.
- **Visibility switch**: control the status bar under Settings → General → Display.

## How it works

- The plugin is notified whenever a round ends and context usage changes, then refreshes the status bar.
- Values come from what the current conversation has recorded: only observed fields are shown, unknown fields stay empty, and real zeros are kept.
- Refreshing the page re-reads the current conversation; reloading the plugin or restarting the process re-collects the history.
- The status bar reuses the platform theme and adds no custom colors, gradients or shadows.

## Notes

- The values reflect observed context usage, not a provider bill.
- Context can drop because of compaction; it does not mean a turn consumed a negative amount.
- Data is isolated per conversation, so switching conversations shows the matching usage.
