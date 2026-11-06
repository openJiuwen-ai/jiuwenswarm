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

- Every UI position is declared in `ui.items` inside `manifest.json`, naming where it goes and which renderer draws it.
- Once the plugin is installed and active, the page places each contribution accordingly, and the plugin backend supplies the content when the page opens.
- The Workbench Files tab reads the current conversation directory, so its content follows the conversation.
- Application-scoped positions appear without a conversation; conversation-scoped positions appear inside a real conversation.

## Notes

- The example calls no model or external business API; it only demonstrates slots, components and interaction protocols.
- The main page, task page and web preview use `Iframe`; the file tab uses `FileTree` and `FilePreview`.
- The backend sends complete view trees, and components handle expansion, selection and hints themselves.
