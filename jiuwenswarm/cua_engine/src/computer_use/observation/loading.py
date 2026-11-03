"""Positive read-refresh evidence, independent of presentation and input.

A provider's busy flag describes that node, not the entire working window.
Missing flags, an empty tree and a black image never establish readiness.
"""
from collections.abc import Mapping
from math import isfinite

from ..native_semantics import field_value


def window_content_unexposed(snapshot):
    """A coverage observation, not a claim that a provider is loading/empty.

    Use native client geometry and the collected raw tree, never a product
    name, a node-count threshold, a compressed tree or the colour of pixels.
    A window with only chrome/unnamed layout may have an unexposed body.
    Nothing is removed and input authority is never derived here.
    """
    if snapshot is None or snapshot.native['platform'] != 'windows':
        return False
    raw = snapshot.native
    if raw['stopReasons'] or raw['unread']:
        return False  # Existing truncation/collection notes explain this view.

    def rect(value):
        if not isinstance(value, Mapping):
            return None
        parts = [value.get(key) for key in ('x', 'y', 'width', 'height')]
        if not all(type(v) in (int, float) and isfinite(v) for v in parts) or min(parts[2:]) <= 0:
            return None
        return parts

    client = next(
        (rect(n['values'].get('Win32.ClientRectangle'))
         for n in raw['nodes'] if n['node'] in raw['roots'] and 'Win32.ClientRectangle' not in n['fieldStatus']), None)
    if client is None:
        return False
    x, y, w, h = client
    for node in raw['nodes']:
        if node['node'] in raw['roots']:
            continue
        values, errors = node['values'], node['fieldStatus']
        role = values.get('ControlType')
        if role in ('ControlType.Window', 'ControlType.TitleBar'):
            continue
        meaningful = (role not in ('ControlType.Pane', 'ControlType.Group', 'ControlType.Custom')
                      or any(values.get(key) for key in ('Name', 'Value.Value', 'Text.DocumentRange'))
                      or bool(node['actions'].get('items')))
        if not meaningful:
            continue
        bounds = rect(values.get('BoundingRectangle')) if 'BoundingRectangle' not in errors else None
        if bounds is None:
            return False  # Cannot locate content; do not classify it as absent.
        bx, by, bw, bh = bounds
        if min(x + w, bx + bw) > max(x, bx) and min(y + h, by + bh) > max(y, by):
            return False
    return True


def busy_nodes(snapshot):
    if snapshot is None:
        return ()
    return tuple(node['node'] for node in snapshot.native['nodes']
                 if field_value(snapshot.native['platform'], node, 'busy') is True)


def initialization_requested(directory, launch):
    # Keep the receipt, not an assumption based on the age/size of a tree.
    # requestReturned can mean an uncertain initializer result; a read may
    # observe its effect, but must neither repeat the setter nor activate.
    return (directory.get('provider', {}).get('status') == 'requestReturned'
            or launch is not None and launch['dispatch'] == 'dispatched')
