# Quick Start

JiuwenClaw supports connecting to the ACP client in VS Code.
Pre-installation Preparation
- Environment Dependencies:
  - Complete the installation of JiuwenClaw
  - Configure the model in the web UI: Settings → Configuration → Model Configuration

**Note: Users may complete installation and configuration based on their actual needs using the following methods.**
**The following example uses VS Code on macOS to connect to the ACP client.**

# VS Code ACP Client Setup (macOS / Linux)
1. Install the ACP Client extension from the marketplace. Search for `formulahendry.acp-client` and install it.

![ACP](../assets/images/acp.png)

2. In the extension, click the `+` button (`ACP: Add Agent Configuration`).

![addAgent](../assets/images/addAgent.png)

3. Under `Add Acp Agent`, enter: jiuwenclaw.

![name](../assets/images/name.png)

4. For `Agent Command`, enter the absolute path to run_gateway_acp.sh.
5. Leave `Agent Arguments` empty.
6. Start the main process in the terminal first: `python -m jiuwenclaw.app`
7. Connect to `jiuwenclaw` in the extension, then chat in the window.

# VS Code ACP Client Setup (Windows)
1. Install the ACP Client extension from the marketplace. Search for `formulahendry.acp-client` and install it.
2. In the extension, click the `+` button (`ACP: Add Agent Configuration`).
3. For `Name`, enter: `jiuwenclaw`.
4. For `Command`, enter the absolute path to run_gateway_acp.cmd.
5. Leave `Config` empty.
6. Start the main process in the terminal first: `python -m jiuwenclaw.app`
7. Connect to `jiuwenclaw` in the extension, then chat in the window.