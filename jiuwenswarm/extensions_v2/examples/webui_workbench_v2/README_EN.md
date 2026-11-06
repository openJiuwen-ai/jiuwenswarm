# Plugin Workbench

A showcase plugin that covers the main WebUI slots and components, demonstrating how plugins inject UI into different parts of the page.

## Features

- **App entry**: a dismissible welcome card on first visit.
- **Sidebar entry**: a Workbench page and a footer user menu.
- **Task area entry**: a Task inspector page.
- **Conversation aids**: quick actions below the input, plus Workbench Files and Web preview tabs on the right.
- **Turn information**: token usage for the current turn, with refresh and details actions.
- **Header status**: a Workbench status next to the conversation title, with a refresh button.

## How it works

1. The plugin declares its UI contributions and slots in `ui.items` inside `manifest.json`.
2. After loading the active package, the WebUI renders each contribution into its declared slot.
3. The plugin backend returns view data through events; the file tab reads the current conversation directory.
4. Application-scoped slots appear without a conversation; conversation-scoped slots appear inside a real conversation.

## Notes

- The example calls no model or external business API; it only demonstrates slots, components and interaction protocols.
- The main page, task page and web preview use `Iframe`; the file tab uses `FileTree` and `FilePreview`.
- The backend sends complete view trees, and components handle expansion, selection and hints themselves.

## Install

From the repository root:

```sh
python scripts/install_workbench_plugin.py --zip-only
```

Install the generated ZIP into the plugin directory, then refresh the page or re-enter the conversation.
