"""Build semantic controls from native facts without changing their identities.

Composition is a hint to the lossless projection, not a licence to remove a
member's independent content or actions. No OS calls or input routes belong here.
"""
from .tree import hasIncompleteChildren, withInteractions
from ..native_semantics import CONTROL_LABEL_ROLES, CONTROL_LAYOUT_ROLES
from .presentation_policy import independent_actions

_LAYOUT = CONTROL_LAYOUT_ROLES
_LABEL = CONTROL_LABEL_ROLES
_EDITORS = {"textField", "textArea", "comboBox"}
_PRIMARY_ACTIONS = {"AXPress", "AXPick", "press", "click", "invoke"}


def composeControls(root, inherited=None, *, fact_backed=False):
    """Return a derived tree; native fields, children and refs stay unchanged.

    editorRef is an observed native owner/editor edge. A text descendant by
    itself is deliberately insufficient evidence for an input composition.
    """
    scoped = withInteractions(root, inherited)

    def visit(source, in_table=False):
        in_table = source["role"] == "table" or (in_table and source["role"] != "outline")
        node = {**source, "children": [visit(child, in_table) for child in source["children"]]}
        if "control" in node:
            # Decoded older observations can already carry composition facts.
            # Never rewrite them or use the resulting display as input evidence.
            return node
        if hasIncompleteChildren(node):
            return node

        editor = node.get("editorRef")
        if editor is not None and editor != node["ref"]:
            path = _editor_path(node, editor)
            if path is not None:
                node["control"] = dict(kind="textInput", members=[
                    dict(ref=part["ref"], part="editor" if part["ref"] == editor else "layout") for part in path])
                _editor_capabilities(node, path[-1])
                return node

        clickable = node.get("capabilities", {}).get("click") == "available"
        item = node['role'] == 'row' and not in_table
        if (not clickable and not (fact_backed and item)) or node.get("enabled") is False or node.get("blockedBy"):
            return node
        control_roles = {'button', 'checkBox', 'radioButton', 'switch', 'link', 'tab', 'group'} if fact_backed else {'button'}
        if node["role"] not in control_roles and not item:
            return node

        members, visited, cells, complete = [], 0, 0, True

        def collect(child, depth):
            nonlocal visited, cells, complete
            visited += 1
            if visited > 64 or depth > 8:
                complete = False
                return
            cells += child["role"] == "cell"
            if (hasIncompleteChildren(child) or child.get("incompleteLabelRelation") or child.get("modal")
                    or child.get("blockedBy", []) != node.get("blockedBy", [])):
                return
            if child["role"] in _LABEL and not child["children"]:
                owners = {node["ref"], *(member["ref"] for member in members)}
                if not set(child.get("labelTargets", [])).difference(owners):
                    members.append(dict(ref=child["ref"], part="label"))
            elif (child["role"] in _LAYOUT and not child.get("name") and not child.get("description")
                  and child.get("value") in (None, "") and not child.get("pressable")
                  and not child.get("selectable") and not (independent_actions(child) if fact_backed else child.get("actions"))
                  and not child.get("actionsUnavailable") and "control" not in child):
                members.append(dict(ref=child["ref"], part="layout"))
                for nested in child["children"]:
                    collect(nested, depth + 1)

        for child in node["children"]:
            collect(child, 1)
        if complete and members and (node["role"] != "row" or cells <= 1):
            node["control"] = dict(kind="item" if node["role"] == "row" else "button", members=members)
        return node

    return visit(scoped)


def _editor_path(owner, editor):
    visited = 0

    def find(node, depth):
        nonlocal visited
        visited += 1
        if (visited > 64 or depth > 8 or hasIncompleteChildren(node) or node.get("modal")
                or node.get("blockedBy", []) != owner.get("blockedBy", [])):
            return None
        if node["ref"] == editor:
            return [node] if node["role"] in _EDITORS else None
        if (node["role"] not in _LAYOUT or node.get("name") or node.get("description")
                or "control" in node):
            return None
        for child in node["children"]:
            path = find(child, depth + 1)
            if path is not None:
                return [node, *path]
        return None

    for child in owner["children"]:
        path = find(child, 1)
        if path is not None:
            return path
    return None


def _editor_capabilities(owner, editor):
    """Keep native facts intact; derived routes belong only to the semantic copy."""
    caps, child_caps = owner.get("capabilities"), editor.get("capabilities")
    if caps is None or child_caps is None or editor.get("enabled") is False or editor.get("blockedBy"):
        return
    derived = {**child_caps, **{key: caps[key] for key in ("typeText", "replaceText", "focus")}}
    if not any(action["name"] in _PRIMARY_ACTIONS for action in editor.get("actions", [])):
        derived["click"] = caps["focus"]
    editor["capabilities"] = derived
