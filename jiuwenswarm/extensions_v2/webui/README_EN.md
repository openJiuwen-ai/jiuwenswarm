# WebUI plugin interactions

Plugins publish trees through `ops.ui.render` and bind business actions with node events:

```python
RenderNode(
    "Button",
    {"label": "Refresh"},
    events={"ui.click": {"id": "refresh", "args": {"section": "summary"}}},
)
```

Declaring a component does not enable its browser renderer. Plugin validation covers 16 non-file components.
See the [component contract](../../channels/web/frontend/src/features/webui/docs/specs/S_01_components.md).

All browser interactions use `webui.dispatch`. The host takes the node path and revision from the displayed instance:

```json
{
  "event": "ui.click",
  "session_id": "s1",
  "target": {"slot_id": "conversation.input.below", "package_id": "pkg.demo", "instance_key": "main"},
  "payload": {"node_key": "0", "revision": 3, "action_id": "refresh", "args": {"section": "summary"}, "data": {}}
}
```

The server validates the instance, revision, node, event, action, arguments and data before entering plugin handlers.
Handlers read editable values from `event.event_input.payload["data"]`. The args object must match the declaration.
Input changes carry `{value, isComposing}`, Switch events carry `{checked}`, and Slider changes carry `{value}`.
The initial `ui.ready` request has an empty payload and does not require an existing instance.

Declare a text input as follows:

```python
RenderNode(
    "Input",
    {"value": "Beijing", "ariaLabel": "City", "allowClear": True},
    events={"ui.change": {"id": "city.edit"}},
)
```

The `ui.change` handler receives composition drafts and a committed value with `isComposing=False`.
Check that flag when processing complete text only. The clear button submits an empty string.
The value and defaultValue props initialize the local draft. A valid replace reinitializes it;
a rejected frame preserves the current input. Disabled, read-only and disconnected inputs do not send changes.

Existing action components remain available and use the same request structure; flat interaction payloads are rejected.
Update handlers from `payload["value"]` to `payload["data"]["value"]`. List selections also put key in data,
instead of appending it to declared args. A node cannot declare both action and events.
Downstream frames use schema_version=1.

Application entry, targets without a session, and file components are not supported.
See [F_07](../docs/features/F_07_ui-contributions.md) for request validation and acknowledgements.
