# JiuwenSwarm for JetBrains — Installation

## Prerequisites

JiuwenSwarm must be running locally before the plugin connects:

```bash
jiuwenswarm-start
# WebSocket server opens at ws://127.0.0.1:19000/ws
```

JCEF (Chromium Embedded Framework) must be enabled. If the panel shows blank on first open, enable it via **Help → Find Action → Registry** → `ide.browser.jcef.enabled`, then restart the IDE.

## Installation

### From ZIP (recommended)

1. Download `jiuwenswarm-plugin-0.1.0.zip` from the [releases page](https://github.com/jiuwencortex/jiuwenswarm-ide/releases).
2. Go to **Settings → Plugins → ⚙ → Install Plugin from Disk** and select the ZIP.
3. Restart the IDE.

### From Marketplace

Search **JiuwenSwarm** in **Settings → Plugins → Marketplace** and click Install.
