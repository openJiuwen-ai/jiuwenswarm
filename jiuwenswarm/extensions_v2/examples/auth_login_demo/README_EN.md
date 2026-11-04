# Sign-in and registration example

[中文](README.md)

The example lives in `jiuwenswarm/extensions_v2/examples/auth_login_demo`; `domain: gateway` in its manifest selects the runtime host.

`webui.login-demo` is a Gateway extension package. It owns registration, mock accounts, non-expiring tokens,
the full-page entry tree, responsive artwork, form rules and the sidebar avatar menu.
Layout uses generic components and existing gap tokens; menu placement uses native AntD placement/align.
Form.requiredMark=false hides label asterisks while preserving required validation.
The plugin computes the account initial and sends it as Avatar.props.children. It chooses a stable background from the full name and declares native style values for the background, white text and font size.
Unicode grapheme segmentation uses regex \X, currently supplied through Core → tiktoken. The host does not derive account initials or colors.
Set Avatar.url for a custom image, which takes priority; omit url when no image is configured.
Both inputs declare autoComplete=off. This native attribute expresses a browser preference; Chrome password management
can ignore it, so the page cannot guarantee suppression of password-save prompts or saved-account suggestions.

## Scope

The package requires the local Gateway browser bridge described in [F_07](../../../gateway/extensions_v2/docs/features/F_07_local-login-integration.md).
The frontend, Gateway bridge and this example are delivered in separate commits. The example is explicitly installed,
not a built-in identity service. An older Gateway without the bridge cannot run this ZIP.

No account is preseeded. Registration signs in immediately. Usernames contain 4–64 characters; passwords contain
8–128 characters including an ASCII letter, a number and a special character. The plugin stores salted password
digests, accounts and sessions in the installed copy's data/accounts.sqlite3. Generated data is excluded from the ZIP.
Tokens have no automatic expiry. Sign-out revokes the current token while preserving the account.
Platform projects, conversations and data are shared between accounts: this example does not implement data isolation.

## Package and install

Run from the repository root:

```sh
.venv/bin/python jiuwenswarm/extensions_v2/examples/auth_login_demo/pack.py --output /tmp/webui-login-demo.zip
```

Grant local_storage.write and request.bind.gateway to webui.login-demo under
extensions.gateway.client_grants in an isolated test instance. Install through the existing
/extension install command or GatewayPackageManager.install before the login gate becomes active.
The manifest uses domain: gateway and does not enable the old auth_provider/auth.session gate.
The explicit package allowlist excludes databases, caches and logs.

settings.json selects language zh/en and layout split/center. Reload after changing these settings.
Restart the test Gateway after editing Python page modules to load the updated module contents.
The promotional area is hidden below 992px. Images and the gradient are package assets.

## Flow

Component props follow [S_17](../../docs/specs/S_17_webui-component-props.md): Button uses value,
Text uses type / strong, and Avatar uses src. Form.Item.label and menu item labels remain valid.
After changing repository sources, rebuild the ZIP and update the installed copy; reload alone does not copy sources.
Restart the test Gateway after changing Python page modules. Preserve the installed data/accounts.sqlite3 when updating.

A rejected business request causes the plugin to render the entry tree. Form validation is local and successful
confirmation reuses ui.click. Registration atomically creates an account and its first session, then signs in immediately.
Invalid fields or duplicate usernames keep the registration view without queuing browser credentials. Successful registration or sign-in queues
client.local_storage.set; the frontend reruns business reads with plugin-declared HTTP headers and WebSocket
request_context fields. The plugin validates the token, clears entry and renders the footer menu.
The plugin sets native styles.root.width=200, autoFocus=false and destroyOnHidden=true so reopening does not retain the previous focus highlight. The menu displays a 20px avatar and the verified username in the regular text color, a type=divider separator, and Sign out. The display-only account row uses disabled=true with a label composed from Box, Avatar and Text nodes.
Sign-out revokes the token before queuing deletion and an explicit page reload; the new page request renders the sign-in tree. Existing and new accounts
can sign in repeatedly, and a browser refresh restores a valid token.

There is no ui.submit, browser-storage read API, operation receipt or host-coded authentication field.
with_client_ops composes the public two-level Core Ops API into the selected three-level local_storage API.
Verification and remaining limits are recorded in F_07; mock acceptance is not production identity-service acceptance.

After a successful sign-in or sign-out, the plugin queues the storage change followed by `ops.client.page.reload()`. This requires the separate `page.reload` deployment grant. Storage changes do not reload the page automatically. Rendering uses the merged `UiRenderInput` API and typed targets; menu actions use `ui.select`.

Sign-out queues deletion followed by page.reload. The new page request renders the sign-in view; the plugin does not render an intermediate sign-in tree before reloading.
