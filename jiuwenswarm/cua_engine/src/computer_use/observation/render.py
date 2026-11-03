"""Full text rendering, with separate source-aware projection and line layout."""
import math
import re

import regex

from ..protocol.primitives import trim, utf16_length
from .accounting import RenderedFields
from .action_layout import action_fields
from .compact import display_actions, display_embedded, display_selection, display_url, unexpanded_menu
from .empty_values import compact_empty_values as compact_empty_values  # Compatibility for historical callers.
from .fact_groups import reference_set
from .formatting import json_string, number_string
from .line_layout import layout_lines
from .projection import projectTree, secondary_actions
from .tree import (
    VALUE_PREVIEW_LIMIT,
    carries_value,
    disclosure,
    has_display_value,
    has_text_selection,
    is_active_modal,
    is_dialog,
    is_hidden,
    is_window,
    selected_text_descendants,
    unique,
    value_label,
)

CONVENTIONAL = {
    "button", "popUpButton", "menuButton", "checkBox", "radioButton", "switch", "link", "closeButton", "zoomButton",
    "minimizeButton", "textField", "textArea", "comboBox"
}
STANDARD_CLICK = CONVENTIONAL | {'menuItem', 'menuBarItem'}


def shortened(value, limit):
    if utf16_length(value) <= limit:
        return dict(head=value, tail="", omitted=0)
    head_limit, tail_limit = math.floor(limit / 4), limit - math.floor(limit / 4)
    head, tail, count, tail_index = [], [], 0, 0
    if re.search(r"[\ud800-\udfff]", value):
        value = value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")
    for match in regex.finditer(r"\X", value):
        segment = match[0]
        if count < head_limit:
            head.append(segment)
        else:
            if tail_index < len(tail):
                tail[tail_index] = segment
            else:
                tail.append(segment)
            tail_index = (tail_index + 1) % tail_limit
        count += 1
    if count <= limit:
        return dict(head=value, tail="", omitted=0)
    return dict(head="".join(head), tail="".join(tail[tail_index:] + tail[:tail_index]), omitted=count - limit)


def quoted(text):
    if text is None:
        return "null"
    return re.sub(r"[\x7f-\x9f\u2028\u2029]", lambda m: f"\\u{ord(m[0]):04x}", json_string(text))


def field(text):
    return quoted(text) if (not text or text != trim(text) or re.search(r'[,">\\\x00-\x1f\x7f-\x9f\u2028\u2029]', text)
                            or re.match(r"[<+~|]", text) or re.search(r"\(partial:|\(showing ", text)) else text


def bounded(text, limit):
    part = shortened(text, limit)
    return f"{part['head']}\n[… {part['omitted']} characters omitted …]\n{part['tail']}" if part["omitted"] else text


def preview(text, name, limit):
    part = shortened(text, limit)
    return (f"{name} preview: {field(part['head'])} … [{part['omitted']} characters omitted] … {field(part['tail'])}"
            if part["omitted"] else f"{name}: {field(text)}")


def role(node):
    if is_dialog(node) and node["role"] != "sheet":
        return "dialog"
    if node.get("subrole") == "popover":
        return "popover"
    description = node.get("roleDescription")
    if node["role"] == "row" and (not description or description in ("row", "outline row", "外框行")):
        return "row"
    if node["role"] == "group" and (not description or description in ("group", "组")):
        return "container"
    return field(description) if description else re.sub(r"([a-z])([A-Z])", r"\1 \2", node["role"]).lower()


def partial(node):
    read = node.get("childrenRead", len(node.get("native", node)["children"]))
    reason = node.get("partialReason")
    unread_children = reason is None and node["role"] != "application" and "childCount" in node and read < node[
        "childCount"]
    if unread_children:
        reason = "children not fully read"
    if reason is None:
        return None
    if "childrenRead" in node and "childCount" in node:
        return (f"showing {number_string(node['childrenRead'])} of "
                f"{number_string(node['childCount'])} items; partial: {field(reason)}")
    if "childCount" in node:
        return f"showing {number_string(read)} of {number_string(node['childCount'])} items; partial: {field(reason)}"
    return f"partial: {field(reason)}; total unknown"


def rendered_actions(node, compact=False):
    result = []
    actions = secondary_actions(node)
    for action in display_actions(node, actions) if compact else actions:
        label = action.get("description") or action["name"]
        ambiguous = any(
            other["name"] != action["name"] and (other["name"] == label or other.get("description") == label)
            for other in node.get("actions", []))
        result.append(f"{field(label)} [name: {quoted(action['name'])}]" if ambiguous else field(label))
    return unique(result)


def render_tree(root, context=None):
    context = {} if context is None else context
    compact = bool(context.get('compact'))
    coverage = RenderedFields() if context.get('recordFields') else None

    def record(node, *fields, kind='display', target=None):
        if coverage is not None:
            for field_name in fields:
                coverage.record(node, field_name, kind=kind, target=target)

    focused_ref = context.get("focusedRef")
    projection = projectTree(root, context)
    roots = projection["roots"]
    part_actions = {}
    if context.get('nativeFacts'):
        # Folding a caption changes presentation, never its action target.
        # Keep that source next to the action instead of requiring the model
        # to reconstruct the native graph. Build once for the entire tree.
        for ref, owner in projection['foldedInto'].items():
            source = projection['nativeNodes'][ref]
            part_actions.setdefault(owner, []).append(source)
    lines, refs, labels, focus_labels, summary_labels, reported_focus = [], [], {}, {}, {}, []
    selected, selected_menus, context_refs, selected_descendants = [], [], {}, []
    collapsed_menus = {}
    inline_children = set()

    def visit(node, depth, parent_blocked=None, shared_help=None, in_menu=False):
        nonlocal selected_descendants
        parent_blocked = [] if parent_blocked is None else parent_blocked
        in_menu = in_menu or node["role"] in ("menu", "menuBar", "menuItem", "menuBarItem")
        indent, states, omissions = "\t" * depth, [], []
        line = f"{node['ref']} {role(node)}"
        if is_dialog(node) and node['role'] != 'sheet' or node.get('subrole') == 'popover':
            record(node, 'subrole')
        elif node.get('roleDescription'):
            record(node, 'roleDescription')
        else:
            record(node, 'role')
        caps = node.get("capabilities")
        deferred_click = node.get("enabled") is False and caps is not None and caps.get(
            "click") == "unknown" and not node.get("blockedBy")
        if node.get("enabled") is False:
            states.append("reported disabled" if deferred_click else "disabled")
            record(node, 'enabled')
        if node.get("selected"):
            states.append("selected")
            record(node, 'selected')
        if node.get("toggleState") in ("on", "off", "mixed"):
            states.append(node["toggleState"])
            record(node, "toggleState")
        if node.get("busy") is True:
            states.append("busy")
            record(node, 'busy')
        if caps is not None:
            show_click_state = caps["click"] == "available" and not node.get("selectable") and (
                not compact or node['role'] not in STANDARD_CLICK)
            if show_click_state:
                states.append("clickable")
            show_selectable_state = node.get("selectable") and node.get(
                "enabled") is not False and not node.get("selected") and caps["click"] != "unavailable"
            if show_selectable_state:
                states.append("selectable")
            if caps["typeText"] == "available":
                states.append("text-input")
            elif caps["replaceText"] == "available":
                states.append("replace-only")
            if caps["replaceText"] != caps["typeText"] and caps["replaceText"] != "available":
                states.append("replace " + caps["replaceText"])
            show_focus_state = caps["focus"] == "available" and caps["typeText"] != "available" and node[
                "role"] not in CONVENTIONAL and not node.get("selectable") and (not compact or node['role']
                                                                                not in ('group', 'webArea'))
            if show_focus_state:
                states.append("focusable")
        else:
            if node.get("selectable"):
                if not node.get("selected"):
                    states.append("selectable")
            elif node.get("pressable") and node["role"] not in CONVENTIONAL:
                states.append("clickable")
            show_declared_focus = node.get("focusable") and node["role"] not in CONVENTIONAL and not node.get(
                "selectable") and (not compact or node['role'] not in ('group', 'webArea'))
            if show_declared_focus:
                states.append("focusable")
        if node.get("submenu"):
            states.append("submenu")
        expanded = disclosure(node)
        if expanded is not None:
            states.append("expanded" if expanded else "collapsed")
            record(node, 'expanded' if 'expanded' in node else 'disclosing')
        if node["ref"] == focused_ref:
            states.append("focused")
        elif node.get("focused"):
            states.append("reports focus")
            record(node, 'focused')
        if node.get("modal"):
            states.append("modal")
            record(node, 'modal')
        elif node.get("modal") is False and is_dialog(node):
            states.append("non-modal")
            record(node, 'modal')
        if is_hidden(node):
            states.append("hidden")
            record(node, 'visible')
            record(node, 'showing')
        if node["ref"] == focused_ref and not has_text_selection(node) and node["role"] not in ("application",
                                                                                                "window"):
            selected_descendants = selected_text_descendants(node)
            if selected_descendants:
                states.append("contains selected text")
        if states:
            line += " (" + ", ".join(states) + ")"

        def display(value, name, limit=math.inf, attribute=None):
            encoded = display_embedded(value, field) if compact else None
            if encoded is not None:
                # No full-value claim: accounting keeps the exact field under
                # its source reference in details, including folded captions.
                return encoded
            part = shortened(value, limit)
            if part["omitted"]:
                omissions.append(f"Omitted: {part['omitted']} characters ({name})")
            elif attribute is not None:
                record(node, attribute)
            return field(part["head"] + part["tail"])

        if node.get("name"):
            line += " " + display(node["name"], "Name", attribute='name')
        fields = []
        # Table coordinates are native index ranges, not spreadsheet addresses.
        # Display positive spans using human row/column numbers; retain empty
        # ranges explicitly rather than inventing a position or merged cell.
        for key, label in (("rowRange", "Row"), ("columnRange", "Column")):
            if key in node:
                span = node[key]
                record(node, key)
                if span["length"] == 0:
                    fields.append(f"{label} range (0-based): {span['location']}:0")
                else:
                    start, end = span["location"] + 1, span["location"] + span["length"]
                    fields.append(label + ("s" if start != end else "") + ": " + str(start) +
                                  ("–" + str(end) if start != end else ""))
        for key, label in (("rowCount", "Table rows"), ("columnCount", "Table columns"), ("selectedChildCount",
                                                                                          "Selected children")):
            if key in node:
                fields.append(label + ": " + str(node[key]))
                record(node, key)
        if "index" in node:
            fields.append("Index (0-based): " + str(node["index"]))
            record(node, 'index')
        if deferred_click:
            fields.append("Click: unknown (rechecked on click)")
        if node.get("editorOwnerRef"):
            fields.append("Editor for: " + node["editorOwnerRef"])
        if "disclosureLevel" in node and node["disclosureLevel"] != node.get("outlineDepth"):
            fields.append("Outline level: " + number_string(node["disclosureLevel"]))
            record(node, 'disclosureLevel')
        if node.get("disclosedBy"):
            fields.append("Parent row: " + node["disclosedBy"])
        if node.get("incompleteDisclosureRelation"):
            fields.append("Parent row: outside this observation or unavailable")
        if node.get("placeholder"):
            fields.append("Placeholder: " + display(node["placeholder"], "Placeholder", attribute='placeholder'))
        if "expanded" in node and "disclosing" in node and node["expanded"] != node["disclosing"]:
            fields.append("Disclosure state: " + ("expanded" if node["disclosing"] else "collapsed") +
                          " (native reports disagree)")
            record(node, 'disclosing')
        if caps is not None and node.get("enabled") is not False and not node.get("blockedBy"):
            if caps["click"] != "available" and (node["role"] in (STANDARD_CLICK if compact else CONVENTIONAL)
                                                 or node.get("selectable")):
                fields.append("Click: " + caps["click"])
            if node["role"] in ("textField", "textArea",
                                "comboBox") and caps["typeText"] != "available" and caps["replaceText"] != "available":
                fields.append("Input: " + caps["typeText"])
        if node["ref"] == focused_ref and node.get("focused") is False:
            fields.append("Focus flag: false")
            record(node, 'focused')
        if node.get("description") and node["description"] != node.get("name"):
            fields.append("Description: " + display(node["description"], "Description", attribute='description'))
        show_value = (carries_value(node["native"]) or (node["ref"] == focused_ref and "value" in node)
                      or (has_display_value(node["native"]) and node.get("value") != node.get("name")
                          and node.get("value") != node.get("description")))
        binary = node["role"] in ("checkBox", "switch", "radioButton")
        value = node.get("value")
        if binary and (node.get("valueIsNumber") or value in ("true", "false")):
            value = {"0": "off", "1": "on", "true": "on", "false": "off"}.get(value, value)
        value_parts = shortened(node.get("value", ""), VALUE_PREVIEW_LIMIT)
        embedded_value = display_embedded(value, field) if compact and isinstance(value, str) else None
        block_value = show_value and "value" in node and embedded_value is None and ("\n" in node["value"]
                                                                                     or value_parts["omitted"] > 0)
        if show_value and not block_value:
            natural_text = compact and node['role'] == 'staticText' and not node.get('name') and not node.get(
                'description') and value is not None
            fields.append(("" if natural_text else value_label(node) + ": ") +
                          ("<unavailable>" if value is None else embedded_value or field(value)))
            if embedded_value is None:
                record(node, 'value')
        if node.get("labelTargets"):
            fields.append("Labels for: " + ", ".join(node["labelTargets"]))
        if node.get("incompleteLabelRelation"):
            fields.append("Label relation: partly outside this observation or unavailable")
        selection_range = display_selection(node) if compact else node.get("selectionRange")
        if selection_range is not None:
            fields.append(
                f"Selection: {number_string(selection_range['location'])}:{number_string(selection_range['length'])}")
            record(node, 'selectionRange')
        if "shortcut" in node:
            fields.append("Shortcut: " + display(node["shortcut"], "Shortcut"))
        if "menuMark" in node:
            fields.append("Mark: " + (display(node["menuMark"], "Mark") if node["menuMark"] else "<none>"))
        # The public readText continuation reads Value, not SelectedText. An
        # acquired selection cannot be shortened without another valid entry.
        selected_parts = shortened(node.get("selectedText", ""), math.inf if coverage is not None else 200)
        if node.get("selectedText") and not selected_parts["omitted"]:
            fields.append("Selected text: " + field(node["selectedText"]))
            record(node, 'selectedText')
        fields.extend(
            action_fields(node, part_actions.get(node['ref'], []), lambda source: rendered_actions(source, compact)))
        if node.get("actionsUnavailable"):
            fields.append("Secondary Actions: <unavailable>")
        if node.get("help") and node["help"] not in (shared_help, node.get("name"), node.get("description"),
                                                     node.get("value")):
            fields.append("Help: " + display(node["help"], "Help", attribute='help'))
        child_help = (node["children"][0].get("help") if len(node["children"]) >= 3 and node["children"][0].get("help")
                      and all(child.get("help") == node["children"][0]["help"] for child in node["children"]) else None)
        if child_help:
            fields.append("Children Help: " + display(child_help, "Children Help"))
            if not compact or display_embedded(child_help, field) is None:
                for child in node['children']:
                    record(child, 'help', kind='equivalent', target=node['ref'])
        if node.get("url") and node["url"] not in (node.get("name"), node.get("description"), node.get("value"),
                                                   node.get("help")):
            fields.append("URL: " + (
                display_url(node['url'], field) if compact else display(node["url"], "URL", attribute='url')))
        needs_identifier = node.get("identifier") and not node.get("name") and not node.get("description") and not (
            node["role"] == "staticText" and node.get("value"))
        if needs_identifier:
            fields.append("ID: " + display(node["identifier"], "ID", attribute='identifier'))
        if node.get("blockedBy") and ",".join(node["blockedBy"]) != ",".join(parent_blocked):
            fields.append("Blocked by: " + ", ".join(node["blockedBy"]))
        if fields:
            line += (", " if node.get("name") else " ") + ", ".join(fields)
        lines.append(indent + line)
        refs.append(node["ref"])
        partial_info = partial(node)
        deferred_menu = compact and unexpanded_menu(node)
        if deferred_menu:
            collapsed_menus.setdefault(node['childCount'], []).append(node['ref'])
        if partial_info and not deferred_menu:
            if compact and 'childrenStart' in node:
                start = node.get('childrenStart', 0)
                end = node.get('childrenEnd', start)
                child_coverage = f'{end - start}/{node["childCount"]} read' + (f', offset {start}' if start else '')
                if 'nextChildOffset' in node:
                    child_coverage += (
                        '; partial; '
                        f'app.element({quoted(node["ref"])}).read({{"offset": {node["nextChildOffset"]}}})')
                    inline_children.add(node['ref'])
                elif end == node['childCount']:
                    child_coverage += '; end of direct children'
                else:
                    child_coverage += '; partial (see details)'
                lines.append(f'{indent}\tChildren: {child_coverage}')
            else:
                lines.append(indent + "\tChildren: " + partial_info)
        all_direct_children_read = (compact and not partial_info and not deferred_menu and 'childrenStart' in node
                                    and 'childrenEnd' in node and node['childrenEnd'] == node.get('childCount'))
        if all_direct_children_read:
            lines.append(
                f'{indent}\tChildren: offsets {node["childrenStart"]}–{node["childrenEnd"]}; end of direct children.')
        if "childrenStart" in node and not deferred_menu and not compact:
            end = (
                ""
                if "childrenEnd" not in node
                else "; inspected through offset " + number_string(node["childrenEnd"] - 1)
                if node["childrenEnd"] > node["childrenStart"]
                else "; no direct children inspected"
            )
            complete = "; end of direct children" if "childrenEnd" in node and node["childrenEnd"] == node.get(
                "childCount") else ""
            lines.append((f"{indent}\tChildren: native children offset "
                          f"{number_string(node['childrenStart'])}{end}{complete}; live native order, not "
                          f"a frozen list."))
        for omission in node.get("childOmissions", []):
            lines.append(indent + "\tOmitted: " + omission)
        if "nextChildOffset" in node and not deferred_menu and not compact:
            lines.append((f'{indent}\tContinue: element({quoted(node["ref"])}).read({{"offset": '
                          f'{number_string(node["nextChildOffset"])}}})'))
        else:
            needs_subtree_hint = partial_info and "childrenStart" not in node and node[
                "role"] != "application" and not deferred_menu
            if needs_subtree_hint:
                lines.append(f"{indent}\tInspect subtree: element({quoted(node['ref'])}).read()")
        for omission in omissions:
            lines.append(indent + "\t" + omission)

        def text_block(name, part, continuation=""):
            lines.append(indent + "\t" + name + ":")

            def body(value):
                lines.extend(indent + "\t| " + field(p) for p in value.split("\n"))

            body(part["head"])
            if part["omitted"]:
                lines.append(f"{indent}\tOmitted: {part['omitted']} characters ({name}){continuation}")
                body(part["tail"])

        if block_value:
            continuation = (f"; use element({quoted(node['ref'])}).readText()" if 'readableTextRefs' not in context
                            or node['ref'] in context['readableTextRefs'] else '; full captured value unavailable')
            text_block("Value", value_parts, continuation)
            record(node, 'value', kind='continuation' if value_parts['omitted'] else 'display')
        if selected_parts["omitted"]:
            text_block("Selected text", selected_parts)
        if coverage is not None:
            for attribute in ('description', 'help', 'url'):
                coverage.equivalent_text(node, attribute)
        label = node.get("name") or node.get("description") or ""
        focus_name = shortened(label, 120)
        focus = node["ref"] + " " + role(node) + (
            (", " + preview(label, "Name", 120) if focus_name["omitted"] else " " + field(label)) if label else "")
        if show_value:
            focus += ", " + preview(value, value_label(node), 200) if value is not None else ", Value: <unavailable>"
        if selection_range is not None:
            focus += (f", Selection: {number_string(selection_range['location'])}:"
                      f"{number_string(selection_range['length'])}")
        if node.get("selectedText"):
            focus += ", " + preview(node["selectedText"], "Selected text", 200)
        focus_labels[node["ref"]] = focus
        summary_name = label or node.get("value") or node.get("identifier") or ""
        short = shortened(summary_name, 80)
        summary_labels[node["ref"]] = (
            node["ref"] + " " + role(node) +
            (" " + field(short["head"] + ("…" if short["omitted"] else "") + short["tail"]) if summary_name else "") +
            (" (preview; see tree)" if short["omitted"] else ""))
        if node.get("selected"):
            (selected_menus if in_menu else selected).append(node)
        if node.get("focused"):
            reported_focus.append(node["ref"])
        labels[node["ref"]] = node.get("name") or node.get("description") or node.get("value") or node["role"]
        needs_child_context = (node["role"] in ("group", "cell") and not node.get("name")
                               and not node.get("description") and len(node["children"]) > 1
                               and any(child["role"] in ("staticText", "heading") for child in node["children"]))
        if needs_child_context:
            context_refs[node["ref"]] = unique(
                context_refs.get(node["ref"], []) + [child["ref"] for child in node["children"]])
        for target in node.get("labelTargets", []):
            context_refs.setdefault(target, []).append(node["ref"])
        for child in node["children"]:
            visit(child, depth + 1, node.get("blockedBy"), child_help, in_menu)

    if "app" in context:
        windows = [node for node in roots if is_window(node)]
        header, title_omission = f"App: {field(context['app'])}.", None
        if len(windows) == 1 and windows[0].get("name"):
            part = shortened(windows[0]["name"], 500)
            header = f"Window: {quoted(part['head'] + part['tail'])}, {header}"
            if part["omitted"]:
                title_omission = f"Window title omitted: {part['omitted']} characters"
        lines.extend([
            header,
            f"Tree: full (subtree {context['subtreeRef']} only; ancestors and other window contents not expanded)"
            if context.get("subtreeRef") else
            "Tree: full (native menu query)" if context.get("menuQuery") else "Tree: full (current UI only)"
        ])
        interactions = []

        def contexts(node, window=None):
            owner = node["ref"] if is_window(node) else window
            if is_active_modal(node):
                interactions.append(node["ref"] + " modal" +
                                    (f" in window {window}" if window else " (owner in window relations)"))
            for child in node["children"]:
                contexts(child, owner)

        for node in roots:
            contexts(node)
        if interactions:
            lines.append("Interaction: " + "; ".join(interactions))
        if title_omission:
            lines.append(title_omission)
    for node in roots:
        visit(node, 0)
    if collapsed_menus:
        lines.append('Menu contents not expanded: ' + '; '.join(
            reference_set(items) + ' (0/' + str(count) + ' children)'
            for count, items in collapsed_menus.items()) + '. Use app.element(ref).read() to inspect a menu.')
        inline_children.update(ref for items in collapsed_menus.values() for ref in items)
    if "app" in context:
        if selected:

            def summary(node):
                parts = [summary_labels[node["ref"]]]

                def has_label(child):
                    return bool(
                        child.get("name") or child.get("description") or
                        (child.get("value")
                         and child["role"] in ("staticText", "heading", "textField", "textArea", "comboBox", "cell")))

                def label_children(child):
                    return [member for member in child["children"] if member["role"] != "row"]

                tail = node
                while not has_label(tail) and len(label_children(tail)) == 1:
                    tail = label_children(tail)[0]
                if tail is not node and has_label(tail):
                    parts.append("label: " + summary_labels[tail["ref"]])
                if not has_label(tail) and len(label_children(tail)) > 1:
                    labelled = []

                    def collect(child):
                        if has_label(child):
                            labelled.append(child)
                        else:
                            for member in label_children(child):
                                collect(member)

                    for child in label_children(tail):
                        collect(child)
                    if labelled:
                        parts.append("contents: " + "; ".join(summary_labels[c["ref"]] for c in labelled[:3]) + (
                            f"; {len(labelled) - 3} more labelled contents (in tree)" if len(labelled) > 3 else ""))
                return "; ".join(parts)

            lines.append("Selected: " + "; ".join(summary(n) for n in selected[:8]) +
                         (f"; {len(selected) - 8} more selected items (marked in tree)" if len(selected) > 8 else ""))
        if selected_menus:
            lines.append("Selected menus: " + "; ".join(summary_labels[n["ref"]] for n in selected_menus[:8]) +
                         (f"; {len(selected_menus) - 8} more (marked in tree)" if len(selected_menus) > 8 else ""))
        if root["role"] == "application" and partial(root):
            lines.append("Tree partial: " + partial(root))
        if focused_ref is not None and focused_ref in focus_labels:
            summary = "\nReported focus: " + summary_labels[focused_ref]
            range_ = projection["nativeNodes"].get(focused_ref, {}).get("selectionRange")
            if range_ is not None:
                summary += (f"; text selection: empty at UTF-16 offset {number_string(range_['location'])}"
                            if range_["length"] == 0 else
                            (f"; text selection: {number_string(range_['length'])} UTF-16 units at offset "
                             f"{number_string(range_['location'])}"))
            selections = [focus_labels[node["ref"]] for node in selected_descendants if node["ref"] in focus_labels]
            if selections:
                summary += ". Text selection in descendants (not exact focus): " + "; ".join(selections[:3])
                if len(selections) > 3:
                    summary += f"; {len(selections) - 3} more selected text controls"
            lines.append(summary)
        elif focused_ref is not None and projection["foldedInto"].get(focused_ref, "") in focus_labels:
            lines.append("\nReported focus: inside " + projection["foldedInto"][focused_ref])
        elif focused_ref is not None:
            ancestor = next(
                (element_id for element_id in context.get("focusAncestorRefs", []) if element_id in focus_labels), None
            )
            lines.append(
                f"\nReported focus: inside {focus_labels[ancestor]} (native child is not in the expanded tree)."
                if ancestor else "\nReported focus: outside the expanded tree.")
        elif reported_focus:
            lines.append("\nReported focus: unavailable.")
        internal = []
        for source, owner in projection["foldedInto"].items():
            if source == focused_ref or owner not in focus_labels:
                continue
            if projection["nativeNodes"].get(source, {}).get("focused"):
                internal.append("inside " + owner)
        others = unique([ref for ref in reported_focus if ref != focused_ref] + internal)
        if others:
            lines.append("Focus flags: " + ", ".join(others[:8]) +
                         (f"; {len(others) - 8} more" if len(others) > 8 else "") +
                         " (node reports; not confirmed input focus).")
    laid_out, layout_stages = layout_lines(lines, roots, context, field)
    text = "\n".join(laid_out)
    result = dict(text=text, refs=refs, labels=labels, roots=roots, contextRefs=context_refs, projection=projection)
    if compact:
        result['inlineChildRefs'] = sorted(inline_children)
    if context.get('recordStages'):
        result['layoutStages'] = layout_stages
    if coverage is not None:
        result['fieldCoverage'] = coverage.entries
    return result
