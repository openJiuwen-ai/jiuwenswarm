# System plugin settings

System plugins declare settings menus, sections and items through `application.settings`. Once a plugin is installed and active, its settings appear at the declared locations.

Settings no longer includes a built-in Extensions menu or startup splash settings page. Plugins can provide their own menus or add items to existing settings sections.
Plugin splash playback, previews and theme backgrounds continue to use their existing entry points.

A plugin menu without an ordering anchor is appended after existing menus. Older declarations using `after: "extensions"` also append under the missing-anchor rule, so their menus and settings remain available.

See the [S_18 settings location specification](../../jiuwenswarm/extensions_v2/docs/specs/S_18_settings-location.md) for declaration and placement rules.
