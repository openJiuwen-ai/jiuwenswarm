"""Full text rendering, with separate source-aware projection and line layout."""
import math
import re
import regex

from ..protocol.primitives import trim, utf16Length
from .accounting import RenderedFields
from .action_layout import action_fields
from .formatting import jsonString, numberString
from .projection import projectTree, secondaryActions
from .line_layout import layout_lines
from .empty_values import compactEmptyValues  # Compatibility for historical callers.
from .compact import display_actions, display_selection, display_url, display_embedded, unexpanded_menu
from .fact_groups import reference_set
from .tree import (carriesValue, hasDisplayValue, disclosure, hasTextSelection, isDialog,
                   isWindow, isHidden, isActiveModal, valuePreviewLimit, selectedTextDescendants, valueLabel, unique)

CONVENTIONAL = {"button", "popUpButton", "menuButton", "checkBox", "radioButton", "switch", "link", "closeButton", "zoomButton", "minimizeButton", "textField", "textArea", "comboBox"}
STANDARD_CLICK = CONVENTIONAL | {'menuItem', 'menuBarItem'}


def shortened(value, limit):
    if utf16Length(value) <= limit:
        return dict(head=value, tail="", omitted=0)
    headLimit, tailLimit = math.floor(limit / 4), limit - math.floor(limit / 4)
    head, tail, count, tailIndex = [], [], 0, 0
    if re.search(r"[\ud800-\udfff]", value):
        value = value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")
    for match in regex.finditer(r"\X", value):
        segment = match[0]
        if count < headLimit:
            head.append(segment)
        else:
            if tailIndex < len(tail):
                tail[tailIndex] = segment
            else:
                tail.append(segment)
            tailIndex = (tailIndex + 1) % tailLimit
        count += 1
    if count <= limit:
        return dict(head=value, tail="", omitted=0)
    return dict(head="".join(head), tail="".join(tail[tailIndex:] + tail[:tailIndex]), omitted=count - limit)


def quoted(text):
    if text is None:
        return "null"
    return re.sub(r"[\x7f-\x9f\u2028\u2029]", lambda m: f"\\u{ord(m[0]):04x}", jsonString(text))


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
    if isDialog(node) and node["role"] != "sheet":
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
    if reason is None and node["role"] != "application" and "childCount" in node and read < node["childCount"]:
        reason = "children not fully read"
    if reason is None:
        return None
    if "childrenRead" in node and "childCount" in node:
        return f"showing {numberString(node['childrenRead'])} of {numberString(node['childCount'])} items; partial: {field(reason)}"
    if "childCount" in node:
        return f"showing {numberString(read)} of {numberString(node['childCount'])} items; partial: {field(reason)}"
    return f"partial: {field(reason)}; total unknown"


def renderedActions(node, compact=False):
    result = []
    actions = secondaryActions(node)
    for action in display_actions(node, actions) if compact else actions:
        label = action.get("description") or action["name"]
        ambiguous = any(other["name"] != action["name"] and (other["name"] == label or other.get("description") == label) for other in node.get("actions", []))
        result.append(f"{field(label)} [name: {quoted(action['name'])}]" if ambiguous else field(label))
    return unique(result)


def renderTree(root, context=None):
    context = {} if context is None else context
    compact = bool(context.get('compact'))
    coverage = RenderedFields() if context.get('recordFields') else None
    def record(node, *fields, kind='display', target=None):
        if coverage is not None:
            for field_name in fields:
                coverage.record(node, field_name, kind=kind, target=target)
    focusedRef = context.get("focusedRef")
    projection = projectTree(root, context)
    roots = projection["roots"]
    partActions = {}
    if context.get('nativeFacts'):
        # Folding a caption changes presentation, never its action target.
        # Keep that source next to the action instead of requiring the model
        # to reconstruct the native graph. Build once for the entire tree.
        for ref, owner in projection['foldedInto'].items():
            source = projection['nativeNodes'][ref]
            partActions.setdefault(owner, []).append(source)
    lines, refs, labels, focusLabels, summaryLabels, reportedFocus = [], [], {}, {}, {}, []
    selected, selectedMenus, contextRefs, selectedDescendants = [], [], {}, []
    collapsed_menus = {}
    inline_children = set()

    def visit(node, depth, parentBlocked=None, sharedHelp=None, inMenu=False):
        nonlocal selectedDescendants
        parentBlocked = [] if parentBlocked is None else parentBlocked
        inMenu = inMenu or node["role"] in ("menu", "menuBar", "menuItem", "menuBarItem")
        indent, states, omissions = "\t" * depth, [], []
        line = f"{node['ref']} {role(node)}"
        if isDialog(node) and node['role'] != 'sheet' or node.get('subrole') == 'popover':
            record(node, 'subrole')
        elif node.get('roleDescription'):
            record(node, 'roleDescription')
        else:
            record(node, 'role')
        caps = node.get("capabilities")
        deferredClick = node.get("enabled") is False and caps is not None and caps.get("click") == "unknown" and not node.get("blockedBy")
        if node.get("enabled") is False:
            states.append("reported disabled" if deferredClick else "disabled")
            record(node, 'enabled')
        if node.get("selected"):
            states.append("selected")
            record(node, 'selected')
        if node.get("busy") is True:
            states.append("busy")
            record(node, 'busy')
        if caps is not None:
            if caps["click"] == "available" and not node.get("selectable") and (not compact or node['role'] not in STANDARD_CLICK):
                states.append("clickable")
            if node.get("selectable") and node.get("enabled") is not False and not node.get("selected") and caps["click"] != "unavailable":
                states.append("selectable")
            if caps["typeText"] == "available":
                states.append("text-input")
            elif caps["replaceText"] == "available":
                states.append("replace-only")
            if caps["replaceText"] != caps["typeText"] and caps["replaceText"] != "available":
                states.append("replace " + caps["replaceText"])
            if caps["focus"] == "available" and caps["typeText"] != "available" and node["role"] not in CONVENTIONAL and not node.get("selectable") and (not compact or node['role'] not in ('group', 'webArea')):
                states.append("focusable")
        else:
            if node.get("selectable"):
                if not node.get("selected"):
                    states.append("selectable")
            elif node.get("pressable") and node["role"] not in CONVENTIONAL:
                states.append("clickable")
            if node.get("focusable") and node["role"] not in CONVENTIONAL and not node.get("selectable") and (not compact or node['role'] not in ('group', 'webArea')):
                states.append("focusable")
        if node.get("submenu"):
            states.append("submenu")
        expanded = disclosure(node)
        if expanded is not None:
            states.append("expanded" if expanded else "collapsed")
            record(node, 'expanded' if 'expanded' in node else 'disclosing')
        if node["ref"] == focusedRef:
            states.append("focused")
        elif node.get("focused"):
            states.append("reports focus")
            record(node, 'focused')
        if node.get("modal"):
            states.append("modal")
            record(node, 'modal')
        elif node.get("modal") is False and isDialog(node):
            states.append("non-modal")
            record(node, 'modal')
        if isHidden(node):
            states.append("hidden")
            record(node, 'visible')
            record(node, 'showing')
        if node["ref"] == focusedRef and not hasTextSelection(node) and node["role"] not in ("application", "window"):
            selectedDescendants = selectedTextDescendants(node)
            if selectedDescendants:
                states.append("contains selected text")
        if states:
            line += " (" + ", ".join(states) + ")"

        def display(value, name, limit=math.inf, attribute=None):
            if compact and (encoded := display_embedded(value, field)) is not None:
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
                    fields.append(label + ("s" if start != end else "") + ": " + str(start)
                                  + ("–" + str(end) if start != end else ""))
        for key, label in (("rowCount", "Table rows"), ("columnCount", "Table columns"),
                           ("selectedChildCount", "Selected children")):
            if key in node:
                fields.append(label + ": " + str(node[key]))
                record(node, key)
        if "index" in node:
            fields.append("Index (0-based): " + str(node["index"]))
            record(node, 'index')
        if deferredClick:
            fields.append("Click: unknown (rechecked on click)")
        if node.get("editorOwnerRef"):
            fields.append("Editor for: " + node["editorOwnerRef"])
        if "disclosureLevel" in node and node["disclosureLevel"] != node.get("outlineDepth"):
            fields.append("Outline level: " + numberString(node["disclosureLevel"]))
            record(node, 'disclosureLevel')
        if node.get("disclosedBy"):
            fields.append("Parent row: " + node["disclosedBy"])
        if node.get("incompleteDisclosureRelation"):
            fields.append("Parent row: outside this observation or unavailable")
        if node.get("placeholder"):
            fields.append("Placeholder: " + display(node["placeholder"], "Placeholder", attribute='placeholder'))
        if "expanded" in node and "disclosing" in node and node["expanded"] != node["disclosing"]:
            fields.append("Disclosure state: " + ("expanded" if node["disclosing"] else "collapsed") + " (native reports disagree)")
            record(node, 'disclosing')
        if caps is not None and node.get("enabled") is not False and not node.get("blockedBy"):
            if caps["click"] != "available" and (node["role"] in (STANDARD_CLICK if compact else CONVENTIONAL) or node.get("selectable")):
                fields.append("Click: " + caps["click"])
            if node["role"] in ("textField", "textArea", "comboBox") and caps["typeText"] != "available" and caps["replaceText"] != "available":
                fields.append("Input: " + caps["typeText"])
        if node["ref"] == focusedRef and node.get("focused") is False:
            fields.append("Focus flag: false")
            record(node, 'focused')
        if node.get("description") and node["description"] != node.get("name"):
            fields.append("Description: " + display(node["description"], "Description", attribute='description'))
        showValue = (carriesValue(node["native"]) or (node["ref"] == focusedRef and "value" in node)
                     or (hasDisplayValue(node["native"]) and node.get("value") != node.get("name") and node.get("value") != node.get("description")))
        binary = node["role"] in ("checkBox", "switch", "radioButton")
        value = node.get("value")
        if binary and (node.get("valueIsNumber") or value in ("true", "false")):
            value = {"0": "off", "1": "on", "true": "on", "false": "off"}.get(value, value)
        valueParts = shortened(node.get("value", ""), valuePreviewLimit)
        embeddedValue = display_embedded(value, field) if compact and isinstance(value, str) else None
        blockValue = showValue and "value" in node and embeddedValue is None and ("\n" in node["value"] or valueParts["omitted"] > 0)
        if showValue and not blockValue:
            natural_text = compact and node['role'] == 'staticText' and not node.get('name') and not node.get('description') and value is not None
            fields.append(("" if natural_text else valueLabel(node) + ": ") + ("<unavailable>" if value is None else embeddedValue or field(value)))
            if embeddedValue is None:
                record(node, 'value')
        if node.get("labelTargets"):
            fields.append("Labels for: " + ", ".join(node["labelTargets"]))
        if node.get("incompleteLabelRelation"):
            fields.append("Label relation: partly outside this observation or unavailable")
        selectionRange = display_selection(node) if compact else node.get("selectionRange")
        if selectionRange is not None:
            fields.append(f"Selection: {numberString(selectionRange['location'])}:{numberString(selectionRange['length'])}")
            record(node, 'selectionRange')
        if "shortcut" in node:
            fields.append("Shortcut: " + display(node["shortcut"], "Shortcut"))
        if "menuMark" in node:
            fields.append("Mark: " + (display(node["menuMark"], "Mark") if node["menuMark"] else "<none>"))
        # The public readText continuation reads Value, not SelectedText. An
        # acquired selection cannot be shortened without another valid entry.
        selectedParts = shortened(node.get("selectedText", ""), math.inf if coverage is not None else 200)
        if node.get("selectedText") and not selectedParts["omitted"]:
            fields.append("Selected text: " + field(node["selectedText"]))
            record(node, 'selectedText')
        fields.extend(action_fields(node, partActions.get(node['ref'], []), lambda source: renderedActions(source, compact)))
        if node.get("actionsUnavailable"):
            fields.append("Secondary Actions: <unavailable>")
        if node.get("help") and node["help"] not in (sharedHelp, node.get("name"), node.get("description"), node.get("value")):
            fields.append("Help: " + display(node["help"], "Help", attribute='help'))
        childHelp = (node["children"][0].get("help") if len(node["children"]) >= 3 and node["children"][0].get("help")
                     and all(child.get("help") == node["children"][0]["help"] for child in node["children"]) else None)
        if childHelp:
            fields.append("Children Help: " + display(childHelp, "Children Help"))
            if not compact or display_embedded(childHelp, field) is None:
                for child in node['children']:
                    record(child, 'help', kind='equivalent', target=node['ref'])
        if node.get("url") and node["url"] not in (node.get("name"), node.get("description"), node.get("value"), node.get("help")):
            fields.append("URL: " + (display_url(node['url'], field) if compact else display(node["url"], "URL", attribute='url')))
        if node.get("identifier") and not node.get("name") and not node.get("description") and not (node["role"] == "staticText" and node.get("value")):
            fields.append("ID: " + display(node["identifier"], "ID", attribute='identifier'))
        if node.get("blockedBy") and ",".join(node["blockedBy"]) != ",".join(parentBlocked):
            fields.append("Blocked by: " + ", ".join(node["blockedBy"]))
        if fields:
            line += (", " if node.get("name") else " ") + ", ".join(fields)
        lines.append(indent + line)
        refs.append(node["ref"])
        partialInfo = partial(node)
        deferred_menu = compact and unexpanded_menu(node)
        if deferred_menu:
            collapsed_menus.setdefault(node['childCount'], []).append(node['ref'])
        if partialInfo and not deferred_menu:
            if compact and 'childrenStart' in node:
                start = node.get('childrenStart', 0)
                end = node.get('childrenEnd', start)
                child_coverage = f'{end-start}/{node["childCount"]} read' + (f', offset {start}' if start else '')
                if 'nextChildOffset' in node:
                    child_coverage += ('; partial; '
                        f'app.element({quoted(node["ref"])}).read({{"offset": {node["nextChildOffset"]}}})')
                    inline_children.add(node['ref'])
                elif end == node['childCount']:
                    child_coverage += '; end of direct children'
                else:
                    child_coverage += '; partial (see details)'
                lines.append(f'{indent}\tChildren: {child_coverage}')
            else:
                lines.append(indent + "\tChildren: " + partialInfo)
        if (compact and not partialInfo and not deferred_menu and 'childrenStart' in node
                and 'childrenEnd' in node and node['childrenEnd'] == node.get('childCount')):
            lines.append(f'{indent}\tChildren: offsets {node["childrenStart"]}–{node["childrenEnd"]}; end of direct children.')
        if "childrenStart" in node and not deferred_menu and not compact:
            end = "" if "childrenEnd" not in node else "; inspected through offset " + numberString(node["childrenEnd"] - 1) if node["childrenEnd"] > node["childrenStart"] else "; no direct children inspected"
            complete = "; end of direct children" if "childrenEnd" in node and node["childrenEnd"] == node.get("childCount") else ""
            lines.append(f"{indent}\tChildren: native children offset {numberString(node['childrenStart'])}{end}{complete}; live native order, not a frozen list.")
        for omission in node.get("childOmissions", []):
            lines.append(indent + "\tOmitted: " + omission)
        if "nextChildOffset" in node and not deferred_menu and not compact:
            lines.append(f'{indent}\tContinue: element({quoted(node["ref"])}).read({{"offset": {numberString(node["nextChildOffset"])}}})')
        elif partialInfo and "childrenStart" not in node and node["role"] != "application" and not deferred_menu:
            lines.append(f"{indent}\tInspect subtree: element({quoted(node['ref'])}).read()")
        for omission in omissions:
            lines.append(indent + "\t" + omission)

        def textBlock(name, part, continuation=""):
            lines.append(indent + "\t" + name + ":")
            def body(value):
                lines.extend(indent + "\t| " + field(p) for p in value.split("\n"))
            body(part["head"])
            if part["omitted"]:
                lines.append(f"{indent}\tOmitted: {part['omitted']} characters ({name}){continuation}")
                body(part["tail"])
        if blockValue:
            continuation = (f"; use element({quoted(node['ref'])}).readText()"
                            if 'readableTextRefs' not in context or node['ref'] in context['readableTextRefs']
                            else '; full captured value unavailable')
            textBlock("Value", valueParts, continuation)
            record(node, 'value', kind='continuation' if valueParts['omitted'] else 'display')
        if selectedParts["omitted"]:
            textBlock("Selected text", selectedParts)
        if coverage is not None:
            for attribute in ('description', 'help', 'url'):
                coverage.equivalent_text(node, attribute)
        label = node.get("name") or node.get("description") or ""
        focusName = shortened(label, 120)
        focus = node["ref"] + " " + role(node) + ((", " + preview(label, "Name", 120) if focusName["omitted"] else " " + field(label)) if label else "")
        if showValue:
            focus += ", " + preview(value, valueLabel(node), 200) if value is not None else ", Value: <unavailable>"
        if selectionRange is not None:
            focus += f", Selection: {numberString(selectionRange['location'])}:{numberString(selectionRange['length'])}"
        if node.get("selectedText"):
            focus += ", " + preview(node["selectedText"], "Selected text", 200)
        focusLabels[node["ref"]] = focus
        summaryName = label or node.get("value") or node.get("identifier") or ""
        short = shortened(summaryName, 80)
        summaryLabels[node["ref"]] = (node["ref"] + " " + role(node) + (" " + field(short["head"] + ("…" if short["omitted"] else "") + short["tail"]) if summaryName else "")
                                      + (" (preview; see tree)" if short["omitted"] else ""))
        if node.get("selected"):
            (selectedMenus if inMenu else selected).append(node)
        if node.get("focused"):
            reportedFocus.append(node["ref"])
        labels[node["ref"]] = node.get("name") or node.get("description") or node.get("value") or node["role"]
        if (node["role"] in ("group", "cell") and not node.get("name") and not node.get("description") and len(node["children"]) > 1
                and any(child["role"] in ("staticText", "heading") for child in node["children"])):
            contextRefs[node["ref"]] = unique(contextRefs.get(node["ref"], []) + [child["ref"] for child in node["children"]])
        for target in node.get("labelTargets", []):
            contextRefs.setdefault(target, []).append(node["ref"])
        for child in node["children"]:
            visit(child, depth + 1, node.get("blockedBy"), childHelp, inMenu)

    if "app" in context:
        windows = [node for node in roots if isWindow(node)]
        header, titleOmission = f"App: {field(context['app'])}.", None
        if len(windows) == 1 and windows[0].get("name"):
            part = shortened(windows[0]["name"], 500)
            header = f"Window: {quoted(part['head'] + part['tail'])}, {header}"
            if part["omitted"]:
                titleOmission = f"Window title omitted: {part['omitted']} characters"
        lines.extend([header, f"Tree: full (subtree {context['subtreeRef']} only; ancestors and other window contents not expanded)" if context.get("subtreeRef") else "Tree: full (native menu query)" if context.get("menuQuery") else "Tree: full (current UI only)"])
        interactions = []
        def contexts(node, window=None):
            owner = node["ref"] if isWindow(node) else window
            if isActiveModal(node):
                interactions.append(node["ref"] + " modal" + (f" in window {window}" if window else " (owner in window relations)"))
            for child in node["children"]:
                contexts(child, owner)
        for node in roots:
            contexts(node)
        if interactions:
            lines.append("Interaction: " + "; ".join(interactions))
        if titleOmission:
            lines.append(titleOmission)
    for node in roots:
        visit(node, 0)
    if collapsed_menus:
        lines.append('Menu contents not expanded: ' + '; '.join(reference_set(items) + ' (0/' + str(count) + ' children)'
                     for count, items in collapsed_menus.items()) + '. Use app.element(ref).read() to inspect a menu.')
        inline_children.update(ref for items in collapsed_menus.values() for ref in items)
    if "app" in context:
        if selected:
            def summary(node):
                parts = [summaryLabels[node["ref"]]]
                def hasLabel(child):
                    return bool(child.get("name") or child.get("description") or (child.get("value") and child["role"] in ("staticText", "heading", "textField", "textArea", "comboBox", "cell")))
                def labelChildren(child): return [member for member in child["children"] if member["role"] != "row"]
                tail = node
                while not hasLabel(tail) and len(labelChildren(tail)) == 1:
                    tail = labelChildren(tail)[0]
                if tail is not node and hasLabel(tail):
                    parts.append("label: " + summaryLabels[tail["ref"]])
                if not hasLabel(tail) and len(labelChildren(tail)) > 1:
                    labelled = []
                    def collect(child):
                        if hasLabel(child):
                            labelled.append(child)
                        else:
                            for member in labelChildren(child):
                                collect(member)
                    for child in labelChildren(tail):
                        collect(child)
                    if labelled:
                        parts.append("contents: " + "; ".join(summaryLabels[c["ref"]] for c in labelled[:3]) + (f"; {len(labelled) - 3} more labelled contents (in tree)" if len(labelled) > 3 else ""))
                return "; ".join(parts)
            lines.append("Selected: " + "; ".join(summary(n) for n in selected[:8]) + (f"; {len(selected) - 8} more selected items (marked in tree)" if len(selected) > 8 else ""))
        if selectedMenus:
            lines.append("Selected menus: " + "; ".join(summaryLabels[n["ref"]] for n in selectedMenus[:8]) + (f"; {len(selectedMenus) - 8} more (marked in tree)" if len(selectedMenus) > 8 else ""))
        if root["role"] == "application" and partial(root):
            lines.append("Tree partial: " + partial(root))
        if focusedRef is not None and focusedRef in focusLabels:
            summary = "\nReported focus: " + summaryLabels[focusedRef]
            range_ = projection["nativeNodes"].get(focusedRef, {}).get("selectionRange")
            if range_ is not None:
                summary += (f"; text selection: empty at UTF-16 offset {numberString(range_['location'])}" if range_["length"] == 0
                            else f"; text selection: {numberString(range_['length'])} UTF-16 units at offset {numberString(range_['location'])}")
            selections = [focusLabels[node["ref"]] for node in selectedDescendants if node["ref"] in focusLabels]
            if selections:
                summary += ". Text selection in descendants (not exact focus): " + "; ".join(selections[:3])
                if len(selections) > 3:
                    summary += f"; {len(selections) - 3} more selected text controls"
            lines.append(summary)
        elif focusedRef is not None and projection["foldedInto"].get(focusedRef, "") in focusLabels:
            lines.append("\nReported focus: inside " + projection["foldedInto"][focusedRef])
        elif focusedRef is not None:
            ancestor = next((id for id in context.get("focusAncestorRefs", []) if id in focusLabels), None)
            lines.append(f"\nReported focus: inside {focusLabels[ancestor]} (native child is not in the expanded tree)." if ancestor else "\nReported focus: outside the expanded tree.")
        elif reportedFocus:
            lines.append("\nReported focus: unavailable.")
        internal = ["inside " + owner for source, owner in projection["foldedInto"].items()
                    if source != focusedRef and projection["nativeNodes"].get(source, {}).get("focused") and owner in focusLabels]
        others = unique([ref for ref in reportedFocus if ref != focusedRef] + internal)
        if others:
            lines.append("Focus flags: " + ", ".join(others[:8]) + (f"; {len(others) - 8} more" if len(others) > 8 else "") + " (node reports; not confirmed input focus).")
    laid_out, layout_stages = layout_lines(lines, roots, context, field)
    text = "\n".join(laid_out)
    result = dict(text=text, refs=refs, labels=labels, roots=roots, contextRefs=contextRefs, projection=projection)
    if compact:
        result['inlineChildRefs'] = sorted(inline_children)
    if context.get('recordStages'):
        result['layoutStages'] = layout_stages
    if coverage is not None:
        result['fieldCoverage'] = coverage.entries
    return result
