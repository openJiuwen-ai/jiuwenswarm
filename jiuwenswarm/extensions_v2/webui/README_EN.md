# WebUI plugin interactions

Plugins publish trees through `ops.ui.render` and bind business actions with node events:

```python
RenderNode(
    "Button",
    {"label": "Refresh"},
    events={"ui.click": {"id": "refresh", "args": {"section": "summary"}}},
)
```

Declaring a component does not enable its browser renderer. Plugin validation covers 20 catalog components (including `Avatar`, `FileTree`, `FilePreview` and `Dropdown`).
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
The initial `ui.ready` normally has an empty payload and requires no existing instance; `application.entry` uses `{"reason": "first_visit"}`.

Declare a text input as follows:

```python
RenderNode(
    "Input",
    {"value": "Beijing", "ariaLabel": "City", "allowClear": True},
    events={
        "ui.change": {"id": "city.edit"},
        "ui.press_enter": {"id": "city.submit"},
    },
)
```

The `ui.change` handler receives composition drafts and a committed value with `isComposing=False`.
Check that flag when processing complete text only. The clear button submits an empty string.
The value and defaultValue props initialize the local draft. A valid replace reinitializes it;
a rejected frame preserves the current input. Disabled, read-only and disconnected inputs do not send changes.

The `ui.press_enter` handler receives `{value}` with the current draft when Enter is pressed, including an empty string.
Bind it independently of change; an input can declare Enter alone. Enter used to confirm composition does not submit.
Disabled, read-only and disconnected inputs do not submit. Each valid submission is sent without waiting for prior receipts.

Bind `"ui.click": {"id": "city.inspect"}` to receive input clicks with the current draft as `{value}`.
Read-only inputs allow clicks. Disabled or disconnected inputs do not send them; unbound clicks keep the default input behavior.

Declare a switch as follows:

```python
RenderNode(
    "Switch",
    {"defaultChecked": True, "ariaLabel": "Enabled"},
    events={"ui.change": {"id": "enabled.change"}},
)
```

The handler reads the boolean checked value from data. checked takes precedence over defaultChecked.
A valid replace reinitializes the state without sending change. Disabled, loading or disconnected switches are disabled;
without a change binding, the switch can still update its local state.
Bind `"ui.click": {"id": "enabled.inspect"}` independently to receive the new `{checked}` state after a click.
When both events are bound, a click sends change followed by click. Bind change alone to handle state changes only.

A select can bind selection, search and clear actions together:

```python
RenderNode(
    "Select",
    {"options": [{"value": "bj", "label": "Beijing"}], "showSearch": True, "allowClear": True},
    events={
        "ui.select": {"id": "city.choose"},
        "ui.search": {"id": "city.search"},
        "ui.clear": {"id": "city.clear"},
    },
)
```

These actions send `{value}`, `{searchValue}` and `{}` respectively. Each binding is optional.
In multiple mode, select still sends the single option just selected. Selection updates locally without a change event.
value takes precedence over defaultValue; a valid replace reinitializes selection. Disabled or disconnected selects cannot be used.

A tree select can bind node selection, the whole selection value and clearing separately:

```python
RenderNode(
    "TreeSelect",
    {"treeData": [{"value": "root", "title": "All", "children": [{"value": "bj", "title": "Beijing"}]}],
     "allowClear": True},
    events={
        "ui.select": {"id": "node.choose"},
        "ui.change": {"id": "node.change"},
        "ui.clear": {"id": "node.clear"},
    },
)
```

Selecting a node sends that node as `{value}`. change sends the whole selection: a string in single mode,
an array in multiple or treeCheckable mode, and null or an empty array after clearing.
The clear action additionally sends `{}`. Each binding is optional.
treeData keys default to their values and search filters by title; disabled or disconnected tree selects cannot be used.

A slider sends a numeric `{value}` to ui.change while dragging or using the keyboard:

```python
RenderNode(
    "Slider",
    {"value": 20, "min": 0, "max": 100, "step": 1, "ariaLabel": "Intensity"},
    events={"ui.change": {"id": "level.change"}},
)
```

Every valid change is sent independently without merging intermediate values; without a binding the slider still adjusts locally.
The value must match min/max and the step; disabled or disconnected sliders cannot be used.

A tab container sends the active key to ui.change when switching:

```python
RenderNode(
    "Tabs",
    {"items": [
        {"key": "one", "label": "Overview", "children": [RenderNode("Text", {"value": "First page"})]},
        {"key": "two", "label": "Details", "children": [RenderNode("Text", {"value": "Second page"})]},
    ]},
    events={"ui.change": {"id": "tab.change"}},
)
```

Hidden pages do not dispatch events and keep their local state by default; with destroyOnHidden they reinitialize after switching back.
Disabled tabs and disconnected sessions cannot switch, and tab content also counts toward the depth and node budgets.

Legacy action components and legacy event names (`ui.press`, `ui.toggle`, `ui.input`) are removed:
nodes only accept catalog components, and interactions are declared through `events` with `ui.click`,
`ui.change`, `ui.press_enter`, `ui.select`, `ui.search` and `ui.clear`.
Handlers read event data from `payload["data"]`; action declarations are no longer supported.
Downstream frames use schema_version=1.

`application.entry` uses application scope and the current Web connection, without a session. It mounts on first visit and stores a browser visit marker after ready is accepted; later refreshes skip it. Clear or failure closes the entry. The other 13 slots require a real session; `application.menu`, `task.function` and `application.settings` do not mount in a new empty conversation.
See [F_07](../docs/features/F_07_ui-contributions.md) for file components, request validation and acknowledgements.
