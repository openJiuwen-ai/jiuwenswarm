"""Compose only declared parts of a control; retain independent targets."""
from ...native_semantics import TABLE_FIELDS
from ...protocol.primitives import utf16_length
from ..tree import VALUE_PREVIEW_LIMIT, disclosure, has_incomplete_children, is_dialog
from .labels import passive_label
from .policy import layout, ordinary_control, same_blocking, sources


def merge_editors(state, node, scope):
    if node.get('control', {}).get('kind') == 'textInput':
        node['children'] = [c for child in node['children'] for c in compact_input(state, child, node)]


def merge_control_labels(state, node, scope):
    if scope.in_table or not ordinary_control(node):
        return
    if not node.get('name'):
        if 'control' in node:
            parts = []
            for member in node['control']['members']:
                source = state.nodes.get(member['ref'])
                if member['part'] == 'label' and source is not None and utf16_length(source.get(
                        'value', '')) <= VALUE_PREVIEW_LIMIT:
                    parts.extend(sources(source))
        else:
            parts = [part for child in node['children'] for part in (label_parts(state, child, node) or [])]
        if parts:
            state.adopt(node, parts, 'name')
    node['children'] = [c for child in node['children'] for c in compact_label(state, child, node)]


def member_layout(state, node, target, ignore_focus=False, ignore_selected=False):
    owned_row_cell = (node.get('_factBacked') and node['role'] == 'cell' and target['role'] == 'row'
                      and state.member_of(node, target))
    if owned_row_cell:
        # A single-column outline/list item's cell is a presentation part,
        # not a spreadsheet cell. Keep its coordinates/focus as source facts.
        # The control-label pass excludes table rows and multi-cell records.
        if any(node.get(key, {}).get('length', 1) != 1 for key in ('rowRange', 'columnRange')):
            return False
        candidate = {key: value for key, value in node.items() if key not in ('rowRange', 'columnRange', 'index')}
        if candidate.get('titleRef') in state.nodes and state.member_of(state.nodes[candidate['titleRef']], target):
            candidate.pop('titleRef')
        return layout(candidate, ignore_focus, ignore_selected)
    return layout(node, ignore_focus, ignore_selected)


def label_parts(state, node, target):
    confirmed_cell_title = (target["role"] == "row" and node["role"] == "cell" and node.get("titleRef")
                            and state.ancestor_of(node["ref"], node["titleRef"])
                            and not node.get("incompleteLabelRelation") and not node.get("valueExpected")
                            and "value" not in node and same_blocking(node, target) and not node.get("modal")
                            and not is_dialog(node))
    if confirmed_cell_title:
        title = state.nodes.get(node["titleRef"])
        exclusive_short_title = (title is not None and not title.get("incompleteLabelRelation")
                                 and same_blocking(title, target) and len(state.targets.get(title["ref"], {})) == 1
                                 and node["ref"] in state.targets[title["ref"]]
                                 and utf16_length(title.get("value", "")) <= VALUE_PREVIEW_LIMIT)
        if exclusive_short_title:
            return sources(title)
    if "capabilities" in target and not state.member_of(node, target):
        return None
    plain_text_member = (node["role"] == "staticText" and not any(
        node.get(k)
        for k in ("pressable", "selectable", "focusable", "valueExpected", "incompleteLabelRelation", "labelTargets"))
                         and not node["children"] and "partialReason" not in node
                         and utf16_length(node.get("value", "")) <= VALUE_PREVIEW_LIMIT)
    if plain_text_member:
        return sources(node)
    passive_image_member = (
        node["role"] == "image"
        and not any(node.get(k) for k in ("pressable", "selectable", "focusable", "actions", "actionsUnavailable"))
        and not node["children"] and "partialReason" not in node)
    if passive_image_member:
        return []
    if not member_layout(state, node, target, True, True):
        return None
    result = []
    for child in node["children"]:
        parts = label_parts(state, child, target)
        if parts is None:
            return None
        result.extend(parts)
    return result


def compact_label(state, node, target):
    if "capabilities" in target and not state.member_of(node, target):
        return [node]
    if passive_label(state, node, target) and not node["children"] and state.represented(node, target):
        state.fold(node, target, "label")
        return []
    declared_layout = any(m["ref"] == node["ref"] and m["part"] == "layout"
                          for m in target.get("control", {}).get("members", []))
    must_keep_label = ((not declared_layout and label_parts(state, node, target) is None)
                       or not member_layout(state, node, target, True, True) or state.keep(node)
                       or not same_blocking(node, target))
    if must_keep_label:
        return [node]
    repeated_selection = node["role"] == "cell" and target["role"] == "row" and node.get(
        "selected") is True and target.get("selected") is True
    internal_focus = node["role"] == "cell" and ordinary_control(target) and not (
        node["ref"] == state.context.get("focusedRef") and node.get("focused") is False)
    node["children"] = [c for child in node["children"] for c in compact_label(state, child, target)]
    if member_layout(state, node, target, internal_focus,
                     repeated_selection) and (node["ref"] != state.context.get("focusedRef") or internal_focus):
        state.fold(node, target, "layout")
        return node["children"]
    return [node]


def compact_input(state, node, owner):
    member = next((m for m in owner.get("control", {}).get("members", []) if m["ref"] == node["ref"]), None)
    must_keep_editor = member is None or "control" in node or state.keep(node) or not same_blocking(node, owner)
    if must_keep_editor:
        return [node]
    node["children"] = [c for child in node["children"] for c in compact_input(state, child, owner)]
    owner_text = {owner.get("name"), owner.get("description"), owner.get("value")}
    has_independent_information = (
        any(node.get(k) and node[k] not in owner_text
            for k in ("name", "description")) or ("value" in node and node["value"] != owner.get("value"))
        or ("value" in node and bool(node.get("valueIsNumber")) != bool(owner.get("valueIsNumber")))
        or (node.get("valueExpected") and "value" not in node)
        or (node.get("selectable") and not owner.get("selectable"))
        or (node.get("pressable") and not owner.get("pressable")
            and node.get("capabilities", {}).get("click") != owner.get("capabilities", {}).get("click"))
        or any(v == "available" and owner.get("capabilities", {}).get(k) != "available"
               for k, v in node.get("capabilities", {}).items())
        or any(node.get(k) for k in ("actions", "actionsUnavailable", "help", "url")) or
        (node.get("placeholder") and node["placeholder"] != owner.get("placeholder")) or has_incomplete_children(node)
        or any(node.get(k) for k in ("labelTargets", "incompleteLabelRelation", "preserveGroup", "subrole", "modal"))
        or ("selected" in node and node["selected"] != owner.get("selected"))
        or (node.get("enabled") is False and owner.get("enabled") is not False) or node.get("expandable")
        or node.get("submenu") or disclosure(node) is not None or "shortcut" in node or "menuMark" in node
        or "toggleState" in node
        or any(key in node for key in TABLE_FIELDS)
        or (node["ref"] == state.context.get("focusedRef") and node.get("focused") is False))
    if has_independent_information:
        return [node]
    if "selectionRange" in node or "selectedText" in node:
        editor_state_differs = ("value" not in owner or node.get("value") != owner["value"]
                                or ("selectionRange" in owner and owner["selectionRange"] != node.get("selectionRange"))
                                or ("selectedText" in owner and owner["selectedText"] != node.get("selectedText")))
        if editor_state_differs:
            return [node]
        if "selectionRange" in node:
            owner["selectionRange"] = dict(node["selectionRange"])
        if "selectedText" in node:
            owner["selectedText"] = node["selectedText"]
    state.fold(node, owner, "editor" if member["part"] == "editor" else "layout")
    return node["children"]
