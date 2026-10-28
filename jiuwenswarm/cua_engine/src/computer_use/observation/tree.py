"""Observation semantics preserved from observation/tree.ts."""
_TEXT_ROLES = {"textField", "textArea", "comboBox"}
_NON_VALUE_ROLES = {
    "application", "window", "sheet", "group", "splitGroup", "column", "scrollArea", "toolbar", "tabGroup", "outline",
    "list", "button", "image", "closeButton", "zoomButton", "minimizeButton"
}
VALUE_PREVIEW_LIMIT = 1200


def unique(values):
    return list(dict.fromkeys(values))


def is_window(node):
    return node["role"] in ("window", "sheet")


def is_dialog(node):
    return node["role"] == "sheet" or node.get("subrole") in ("dialog", "systemDialog")


def is_hidden(node):
    # Explicitly hidden is different from an off-screen/occluded modal.
    # Unknown or conflicting native visibility cannot waive modal blocking.
    return node.get("visible") is False and node.get("showing") is False


def is_active_modal(node):
    return bool(node.get("modal")) and not is_hidden(node)


def disclosure(node):
    if node.get("expanded") is True or node.get("expandable") or node["role"] in ("row", "disclosureTriangle"):
        if "expanded" in node:
            return node["expanded"]
        if node.get("expandable") or node.get("disclosing") is True:
            return node.get("disclosing")
    return None


def carries_value(node):
    return bool(
        node.get("valueExpected") or node["role"]
        in _TEXT_ROLES | {"slider", "checkBox", "switch", "radioButton", "progressIndicator", "incrementor"}
        or (node["role"] == "cell" and node.get("name")))


def has_display_value(node):
    caps = node.get("capabilities", {})
    return carries_value(node) or ("value" in node and
                                   (node["value"] != "" or node["role"] not in _NON_VALUE_ROLES
                                    or caps.get("typeText") == "available" or caps.get("replaceText") == "available"
                                    or bool(node.get("focused")) or "selectionRange" in node
                                    or bool(node.get("selectedText"))))


def has_incomplete_children(node):
    return (any(k in node for k in ("partialReason", "nextChildOffset", "childrenStart", "childrenEnd"))
            or bool(node.get("childOmissions"))
            or ("childCount" in node and node.get("childrenRead", len(node["children"])) < node["childCount"]))


def has_text_selection(node):
    return node["selectionRange"]["length"] > 0 if "selectionRange" in node else bool(node.get("selectedText"))


def selected_text_descendants(node):
    result = []
    for child in node["children"]:
        if child["role"] in ("application", "window"):
            continue
        result.extend([child] if has_text_selection(child) else selected_text_descendants(child))
    return result


def value_label(node):

    def has_text(child):
        return ((not child.get("valueIsNumber") and "value" in child and child["role"] in _TEXT_ROLES | {"staticText"})
                or any(has_text(c) for c in child["children"]))

    return "Native value" if node.get("valueIsNumber") and node["role"] in ("group", "cell", "row", "unknown",
                                                                            "textField") and any(
                                                                                has_text(c)
                                                                                for c in node["children"]) else "Value"


def with_interactions(root, inherited=None):
    inherited = [] if inherited is None else inherited

    def modals(node):
        if is_active_modal(node):
            return [node["ref"]]
        if is_window(node):
            return []
        return [ref for child in node["children"] for ref in modals(child)]

    own = sorted(unique(root.get("ownedModals", []) +
                        [ref for child in root["children"]
                         for ref in modals(child)])) if is_window(root) or is_active_modal(root) else []
    incoming = [] if root["ref"] in inherited or root["role"] == "window" else inherited
    blocked = sorted(unique(incoming + own))
    return {
        **root, "blockedBy":
        blocked, **({
            "capabilities": {k: "unavailable"
                             for k in ("click", "typeText", "replaceText", "focus")}
        } if "capabilities" in root and blocked else {}), "children":
        [with_interactions(child, blocked) for child in root["children"]]
    }


def interaction_view(root, include_background):
    node = root
    if not include_background and root.get("blockedBy"):

        def contains_modal(child):
            return child["ref"] in root["blockedBy"] or any(contains_modal(c) for c in child["children"])

        children = [child for child in root["children"] if contains_modal(child)]
        if children and len(children) != len(root["children"]):
            node = {
                **root, "children":
                children, "partialReason": (f"background blocked by {', '.join(root['blockedBy'])}; select this window "
                                            f"explicitly to inspect its background")
            }
            node.pop("childrenRead", None)
            node.pop("childCount", None)
    return {**node, "children": [interaction_view(c, include_background) for c in node["children"]]}
