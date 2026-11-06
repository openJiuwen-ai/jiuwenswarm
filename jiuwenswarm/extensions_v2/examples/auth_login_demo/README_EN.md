# Auth and sign-in

Provides complete sign-in and sign-up pages at the application entry, plus an account menu in the sidebar.

## Features

- **Sign-in and sign-up**: the entry page appears first; a successful registration signs you in.
- **Credential injection**: authenticated HTTP and WebSocket requests receive the credential automatically.
- **Account menu**: the sidebar footer shows the current account, avatar and sign-out entry.
- **Immediate revocation**: signing out clears the local credential and revokes the current token.

## How it works

- The Gateway loads the plugin. The first visit shows the sign-in page, and the main app opens after signing in or registering.
- After signing in, the credential stays in the current browser, and later HTTP and WebSocket requests carry it automatically.
- The plugin checks the session on the request path and rejects requests without a valid credential.
- The sidebar footer shows the current account and a sign-out entry; signing out clears the local credential and revokes the token.

## Notes

- Accounts and tokens live in `data/accounts.sqlite3` inside the installed copy, not in the source package.
- Accounts do not participate in platform data routing; different accounts see the same existing projects, conversations and data.
- This example demonstrates plugin-owned authentication and is not the platform's built-in authentication.
