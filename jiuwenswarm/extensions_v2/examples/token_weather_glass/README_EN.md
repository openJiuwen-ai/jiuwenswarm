# TokenWeather bar

The plugin and settings use the name `TokenWeather` in both Chinese and English. The bar starts with a weather symbol and state instead of repeating the brand title.

`webui.token-weather-glass` is an independent AgentServer-domain V2 plugin. It requires a host with client boolean preferences and settings location support. The original Token Weather and Workbench packages remain separate.

The bar above the input shows observed context tokens, window size and percentage from `ops.session.usage()`, plus rate limits and USD cost when provided. Unknown readings are omitted; reported zeros are retained. Usage events refresh mounted conversations and ready requests query the current snapshot. This is not a provider billing ledger.

Formatting follows the host context popover: used tokens use decimal K, limits use binary K/M, and values show at most one decimal place without trailing zeros. When tokens and the limit are known, their ratio supplies the percentage, for example `8.9% · 23.2K/256K tokens`.

The forecast uses unrounded context occupancy: `☀ Clear` below 25%, `☁ Cloudy` from 25% to below 50%, `☂ Showers` from 50% to below 75%, `☇ Storm` from 75% to below 90%, and `↯ Compact soon` at 90% or above. It shares the displayed percentage's source. Unknown occupancy has no forecast; a reported zero is Clear. “Compact soon” indicates high occupancy, not a guarantee of the host's compaction timing.

The bar reuses shared host components and theme styles without declaring custom colors, gradients, blur, corner radii or shadows. “Open details” opens a separate plugin tab in the right panel.

## Trends and details

The bar includes a context sparkline for the last 12 observed turns and the last context delta. Context drops may reflect compaction; they are not negative consumption. Open details explicitly opens or restores this session's plugin tab on the calling client. Background updates never change the selected tab.

The plugin supplies separate Chinese and English iframe documents, without external resources or scripts. The page combines an overview, stacked context composition bars by turn, and the existing exact input/output/cache/total/context table. The existing `ops.session.usage(breakdown="summary")` query supplies same-turn category snapshots. System prompt, tools and connectors, skills, and messages follow the popover's order and colors. Hover or focus a bar to inspect exact values. Category sums are not rescaled to match overall context; missing observations are labeled and reported zero remains zero. Cached input is included in input, never added again. Repeated request IDs update retained bars without adding a turn; evicted requests cannot reenter the chart. Browser refresh, hiding the bar, and closing the tab retain in-process history; plugin or process reload starts collecting again. Neither private host logs nor current categories are used to invent earlier history.

Requires ops.ui.create_resource_dir / asset_url / release_resources, localized Iframe.src URLs and titles, and Button.openPanel. The plugin packages its HTML template and CSS in assets/, generates immutable detail files, and sends only resource URLs in WebUI frames. All charts and usage logic belong to the plugin; host changes are generic rendering and navigation capabilities.

Details declare `Iframe.minWidth: 360`. The host provides horizontal scrolling below 360px and fills wider panels without a width mode selector.

## Display preference

The manifest uses `type: web.slot.contribution`, `slot: application.settings`, `scope: application` and location to contribute an item at Settings → General → Display. This is the same settings entry used for plugin menus and sections. It defaults to on. Turning it off hides both the bar and an open Usage details tab while retaining the settings switch, observed history and background updates. Enabling it again does not select the details tab automatically. The host must support UiRenderInput.visible_when. The package-isolated preference persists on the current browser/desktop client across conversations and restarts. Same-origin tabs synchronize; devices do not.

Settings metadata, usage text, button labels and accessible labels have separate Chinese and English translations. They update with the host interface language while retaining the display preference and observations. When only the window capacity is known, the bar shows “Context limit”; it does not present that capacity as consumed tokens. A failed preference write displays a host-localized error and retains the saved value.

## Package and install

From the repository root:

```bash
uv run python jiuwenswarm/extensions_v2/examples/token_weather_glass/pack.py --output /tmp/token-weather-glass.zip
```

Install the ZIP using the existing V2 package installation entry. The manifest is at the ZIP root. Source presence does not enable the plugin automatically; the running host must include the required interfaces. Uninstalling preserves the client display preference.

Targeted tests exercise the real manager/EventBus/Ops path with controlled observations, sessionless settings, custom-style rejection, panel opening and preference lifecycles. The frontend fixture at `tests/fixtures/token-weather-glass/index.html` replays controlled usage/catalog data through production components. It is not live model end-to-end verification. Current evidence and limitations are recorded in [F_33](../../docs/features/F_33_token-weather-glass.md). The generic settings contract is [S_18](../../docs/specs/S_18_settings-location.md).
