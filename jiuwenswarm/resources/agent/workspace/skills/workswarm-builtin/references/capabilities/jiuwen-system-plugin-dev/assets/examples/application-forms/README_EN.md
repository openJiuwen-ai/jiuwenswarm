# Application Forms

Turn the JiuwenClaw workspace into a compact desktop companion to follow conversation progress and keep chatting alongside other work.

## Features

- **Bee companion**: a built-in animated bee shows thinking, tool use, replies, and completion. Click to view conversations, use + to start a new one, or drag to move it.
- **Desktop conversations**: read the current user turn and its complete reply, send messages, stop execution, and choose a model. Return to the workspace for full controls.
- **Custom forms**: import resource packages containing an interface, interactions, and assets, with independently saved settings. Capsule cards are an optional import with dark and light appearances; they are not included in the plugin ZIP.
- **Transitions and sounds**: move between the bee and workspace with a reversible animation. Choose Whoosh or Soft pop, adjust the volume, preview the sounds, or turn them off.

## How it works

Once enabled, `ui.ready` registers available forms and loads preferences. Choose a form through the application's form switcher to open its desktop window. Importing resources and saving preferences do not switch forms.

`ui.change` handles resource imports and preference changes, `ui.click` removes resources or resets image settings, and `ui.press_enter` saves resource text settings. Conversation status and actions connect to existing app conversations. Double-click the bee or choose Open workspace to return to the full interface.

Floating desktop windows and transitions require a JiuwenClaw desktop client supporting application forms and the window-transition protocol. Tool approvals, questions requiring confirmation, and full history remain in the workspace.

## Benefits

Expand a small window to check progress or reply while working in other desktop apps. Use the built-in bee or import a form that suits your workflow. Resource settings and sound preferences persist across restarts.
