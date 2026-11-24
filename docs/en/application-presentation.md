# Application forms and in-app overlays

Gateway plugins publish an `ApplicationSurface` in `application.presentation` with an explicit host mode:

| mode | Behavior |
| --- | --- |
| desktop (default) | The desktop client opens a transparent native window and hides the workspace after ready |
| overlay | Browsers and desktop clients display a floating surface inside the app while keeping the workspace visible |

Construct the root in the plugin's existing render handler. Use the actual resource URL published by the current package activation:

```python
root = RenderNode("ApplicationSurface", {
    "title": {"zh": "我的桌宠", "en": "My companion"},
    "src": resource_url,
    "mode": "overlay",
    "autoStart": True,
    "width": 160,
    "height": 120,
})
```

Declare the Gateway domain and this slot. An overlay with `autoStart: True` appears on installation or enable and closes on disable, without a form-menu selection. Reloading the app mounts it again.
`autoStart` defaults to False and supports both modes; desktop mode still requires the desktop client. Other candidates use the application form menu or the existing `presentation query/start` command.
One connection runs one active form. Existing resources without mode continue to use desktop.

Plugins managing their own visibility use controlled `Switch.checked` and `ui.change` to send actions to their backend. Use `ops.ui.render` to publish a surface with `autoStart: True`, and the same Ops with CLEAR to disable it. DELIVERED confirms frame delivery, not container readiness. Open failures report `ui.closed` so the plugin can turn its switch off. Starting the current conversation retains the current page.

Closing destroys the physical container first, then reports `ui.closed` through `webui.dispatch` with reason, revision and presentation_id. Gateway validates the original connection, package, instance, exact revision and container sequence. Candidates remain published; their plugin decides whether to CLEAR them, updates display state and renders settings through `ui.render`. Stale closes cannot overwrite a newer form, and older plugins can reopen the same candidate. `visible_when` only controls visibility; the host does not reverse-write preferences. Form selection, host mode and visibility policy belong to the plugin.

## Dragging a resource

Keep the `workswarm.presentation` v1 sandbox protocol: register the message listener, then send ready to parent.
The host sends state. Overlay messages also carry `host.bounds` for the surface rectangle and `host.workArea` for the entire app viewport.
The iframe's window.innerWidth/innerHeight describe its content size and must not define the companion's movement boundary.

Use `setPointerCapture(event.pointerId)` on the drag target and screenX/screenY coordinates that remain stable when the iframe moves.
Round the cumulative displacement, then subtract the previously sent cumulative value to obtain integer move deltas; independently rounding every incremental step introduces drift.
On pointerup, submit the release position using the same calculation before ending the drag.
The host moves the surface and constrains it to the app viewport. Use resize when expanding or collapsing content; the host reports its actual geometry.
See the runnable [drag resource example](../../jiuwenswarm/channels/web/frontend/tests/fixtures/presentationOverlayPet.html).

## Lifecycle and limits

Page and conversation switches retain the iframe. Closing, disabling, uninstalling, disconnecting or resource failure destroys the overlay.
Changing mode or src ends the current activation. Automatic forms display the new declaration; other candidates require an explicit restart. Config updates retain the page.
Closing an automatic overlay keeps it closed through ordinary updates. Disable and enable it, or reload the app, to show it again. An automatic start reports an occupied form instead of replacing it.
Sending, replies and activity use the existing conversation actions. In-app workspace navigates to a conversation while retaining the overlay.
The app receives input outside the surface rectangle. The plugin receives input inside it, including transparent pixels; keep the rectangle compact.
Overlay mode does not support native screenshot transitions or hitTest. These actions return UNSUPPORTED_ACTION.

See [S_17](../../jiuwenswarm/extensions_v2/docs/specs/S_17_webui-component-props.md) and [S_11 in the separate agent-gateway repository](https://gitcode.com/openJiuwen/agent-gateway/blob/develop/agent_gateway/extensions_v2/docs/specs/S_11_application-presentation.md) for the full interface and lifecycle. Both the client and Gateway must include mode / autoStart support.
