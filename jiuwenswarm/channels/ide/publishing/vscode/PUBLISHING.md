# Publishing the VS Code Extension

This guide covers building, packaging, and publishing the JiuwenSwarm VS Code extension to the VS Code Marketplace and the OpenVSX registry.

## Quick Build from Source

If you just want to build the extension locally and install it manually:

```bash
cd packages/vscode-extension
npm install          # installs esbuild + TypeScript dev dependencies
npm run build        # one-shot compile → out/extension.js
npx vsce package --no-dependencies   # → jiuwenswarm-0.1.0.vsix
```

Install the resulting VSIX:

```bash
code --install-extension jiuwenswarm-0.1.0.vsix
```

---

## Development Build

```bash
cd packages/vscode-extension
npm install
npm run build      # one-shot → out/extension.js
npm run watch      # watch mode
```

Open `packages/vscode-extension` in VS Code and press **F5** to launch an Extension Development Host.

## Package for Distribution

```bash
npm install -g @vscode/vsce
npx vsce package --no-dependencies   # → jiuwenswarm-0.1.0.vsix
```

> `--no-dependencies` is used because runtime dependencies (`ws`) are not bundled; the extension connects via the host's native WebSocket.

Install the VSIX locally for testing:

```bash
code --install-extension jiuwenswarm-0.1.0.vsix
```

## VS Code Marketplace

### One-time publisher setup

1. Sign in to [marketplace.visualstudio.com/manage](https://marketplace.visualstudio.com/manage) with a Microsoft account.
2. Click **Create publisher** and use `openjiuwen` as the publisher ID (must match `"publisher"` in `package.json`).

### Generate a Personal Access Token (PAT)

1. Go to [dev.azure.com](https://dev.azure.com) → your organization → **User Settings → Personal access tokens**.
2. Click **New Token**:
   - **Scopes**: Custom defined → **Marketplace → Manage** (or full access)
   - **Expiration**: set to at least 1 year
3. Copy the token.

### Ensure required package.json fields

The Marketplace requires:

```json
{
  "repository": {
    "type": "git",
    "url": "https://github.com/openjiuwen/jiuwenswarm-ide"
  },
  "license": "MIT",
  "homepage": "https://github.com/openjiuwen/jiuwenswarm-ide#readme",
  "galleryBanner": {
    "color": "#1e1e2e",
    "theme": "dark"
  }
}
```

Also ensure `resources/icon.png` exists and is 128×128 px.

### Install vsce and log in

```bash
npm install -g @vscode/vsce
vsce login openjiuwen
# Paste your PAT when prompted
```

### Publish

```bash
cd packages/vscode-extension
npm run build
vsce publish
```

Bump version automatically:

```bash
vsce publish minor   # 0.1.0 → 0.2.0
vsce publish patch   # 0.1.0 → 0.1.1
vsce publish major   # 0.1.0 → 1.0.0
```

Or publish with an explicit token:

```bash
vsce publish --pat "$VSCE_PAT"
```

## OpenVSX

[Open VSX](https://open-vsx.org) is the community registry used by VSCodium, Gitpod, Eclipse Theia, and other VS Code-compatible editors.

### Create an account and namespace

1. Go to [open-vsx.org](https://open-vsx.org) → **Sign in with GitHub**.
2. Navigate to **User Settings → Access Tokens** → generate a token.
3. In the **Namespaces** tab, claim the `openjiuwen` namespace.

### Publish

```bash
npm install -g ovsx
cd packages/vscode-extension
npm run build
vsce package   # produces jiuwenswarm-0.1.0.vsix
ovsx publish jiuwenswarm-0.1.0.vsix --pat "$OVSX_PAT"
```

## CI Automation (GitHub Actions)

Create `.github/workflows/publish-vscode.yml`:

```yaml
name: Publish VS Code Extension

on:
  push:
    tags: ['vscode-v*']

jobs:
  publish:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-node@v4
        with:
          node-version: 20

      - name: Install dependencies
        working-directory: packages/vscode-extension
        run: npm ci

      - name: Build
        working-directory: packages/vscode-extension
        run: npm run build

      - name: Publish to Marketplace
        working-directory: packages/vscode-extension
        run: npx vsce publish --pat "${{ secrets.VSCE_PAT }}"

      - name: Publish to OpenVSX
        working-directory: packages/vscode-extension
        run: |
          npx vsce package
          npx ovsx publish jiuwenswarm-*.vsix --pat "${{ secrets.OVSX_PAT }}"
```

Add `VSCE_PAT` and `OVSX_PAT` as repository secrets.

## Release Checklist

- [ ] Bump `version` in `packages/vscode-extension/package.json`
- [ ] Install the built VSIX locally and smoke-test key flows
- [ ] Tag the release: `git tag vscode-vX.Y.Z`
- [ ] Push tags to remote — CI workflows pick them up automatically
