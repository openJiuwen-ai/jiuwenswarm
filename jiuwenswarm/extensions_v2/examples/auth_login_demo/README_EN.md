# Auth and sign-in

Provides complete sign-in and sign-up pages at the application entry, plus an account menu in the sidebar.

## Features

- **Sign-in and sign-up**: the entry page appears first; a successful registration signs you in.
- **Credential injection**: authenticated HTTP and WebSocket requests receive the credential automatically.
- **Account menu**: the sidebar footer shows the current account, avatar and sign-out entry.
- **Immediate revocation**: signing out clears the local credential and revokes the current token.

## How it works

1. The Gateway loads the plugin; once active it renders the sign-in page at `application.entry`.
2. A successful sign-in stores the credential in the browser's local storage.
3. `requestBindings` injects the credential before requests are sent, and `gateway.request` validates the session.
4. The sidebar footer at `application.sidebar.footer` shows the account menu; signing out revokes the credential.

## Notes

- Accounts and tokens live in `data/accounts.sqlite3` inside the installed copy, not in the source package.
- Accounts do not participate in platform data routing; different accounts see the same existing projects, conversations and data.
- This example demonstrates plugin-owned authentication and is not the platform's built-in authentication.

## Install

From the repository root:

```sh
python jiuwenswarm/extensions_v2/examples/auth_login_demo/pack.py --output /tmp/webui-login-demo.zip
```

Place the ZIP where the Gateway host can read it, then install it from the plugin detail page and reload.
