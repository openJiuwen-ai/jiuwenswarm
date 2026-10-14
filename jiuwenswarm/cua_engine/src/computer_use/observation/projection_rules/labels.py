"""Explicit label ownership and captions. No substring-based deletion."""
from ...protocol.primitives import utf16Length
from ..tree import disclosure, isDialog, valuePreviewLimit
from .policy import sources, content, protectedContent, sameBlocking, ordinaryControl, REGIONS


def promote_description(state, node, scope):
    if not node.get('name') and node.get('description') and (ordinaryControl(node) or node['role'] in REGIONS or node['role'] == 'heading'):
        state.adopt(node, [part for part in node['textSources'] if part['field'] == 'description'], 'name', ' ')
        node['description'] = ''


def merge_region_labels(state, node, scope):
    if node['role'] not in REGIONS or scope.in_table:
        return [node]
    headings = [child for child in node['children'] if child['role'] == 'heading' and child.get('name')]
    if not node.get('name') and len(headings) == 1 and node['children'][0] is headings[0]:
        state.adopt(node, sources(headings[0]), 'name')
    retained = []
    for child in node['children']:
        if passiveLabel(state, child, node) and not child['children'] and state.represented(child, node):
            state.fold(child, node, 'label')
        else:
            retained.append(child)
    node['children'] = retained
    return [node]


def passiveLabel(state, node, target, linked=False):
    return (passive_caption(state, node, target)
            and ("capabilities" not in target or linked or state.memberOf(node, target))
            and (not node.get("labelTargets") or linked))


def passive_caption(state, node, target):
    """A complete caption with no independent interaction or semantic boundary.

    Ownership is checked by the caller: an explicit label relation, a declared
    control member, or an exactly repeated field on the immediate parent.
    """
    return (node["role"] in ("staticText", "heading") and len(content(node)) > 0 and not state.keep(node)
            and node["ref"] != state.context.get("focusedRef") and not any(node.get(k) for k in ("focused", "pressable", "selectable", "focusable", "selected"))
            and node.get("enabled") is not False and disclosure(node) is None and not node.get("expandable") and not node.get("submenu")
            and not node.get("valueExpected") and not protectedContent(node) and not node.get("incompleteLabelRelation")
            and "available" not in node.get("capabilities", {}).values()
            and not any(node.get(k) for k in ("preserveGroup", "subrole", "help", "url", "modal"))
            and "control" not in node and not isDialog(node)
            and utf16Length(node.get("value", "")) <= valuePreviewLimit and sameBlocking(node, target))



def associate_labels(state):
    state.targets, labels = {}, {}
    def add(label, target):
        target_set = state.targets.setdefault(label, {})
        target_set[target] = None
    for node in state.nodes.values():
        if node["role"] == "staticText":
            for target in node.get("labelTargets", []):
                add(node["ref"], target)
        if node.get("titleRef") and state.nodes.get(node["titleRef"], {}).get("role") == "staticText":
            add(node["titleRef"], node["ref"])
    for label, ids in state.targets.items():
        if label in state.nodes:
            state.nodes[label]["labelTargets"] = list(ids)
    for label, ids in state.targets.items():
        for id in ids:
            target = state.nodes.get(id)
            if id != label and target is not None and state.scopes.get(id) == state.scopes.get(label) and target["role"] not in ("staticText", "application", "window", "sheet", "menuBar"):
                labels.setdefault(id, []).append(label)
    consumed, additions = set(), {}

    for id in state.order:
        target, linked = state.nodes[id], labels.get(id)
        if not linked:
            continue
        primary = target["titleRef"] if target.get("titleRef") in linked else linked[0]
        if not target.get("name") and utf16Length(state.nodes[primary].get("value", "")) <= valuePreviewLimit:
            state.adopt(target, sources(state.nodes[primary]), "name", " ")
        for label in linked:
            node = state.nodes[label]
            if utf16Length(node.get("value", "")) > valuePreviewLimit:
                continue
            parts = [part for part in sources(node) if not any(existing["text"] == part["text"] for existing in target["textSources"])]
            if parts:
                state.adopt(target, parts, "description", " ")
        target["linkedLabel"] = True
        for label in linked:
            node = state.nodes[label]
            if (len(state.targets.get(label, {})) == 1 and not state.ancestorOf(label, id) and passiveLabel(state, node, target, True)
                    and state.represented(node, target) and all(child["role"] == "link" and child["ref"] != id for child in node["children"])):
                consumed.add(label)
                state.fold(node, target, "label")
                additions.setdefault(id, []).extend(node["children"])

    def removeConsumed(node):
        node["children"] = [child for child in node["children"] if child["ref"] not in consumed] + additions.get(node["ref"], [])
        for child in node["children"]:
            removeConsumed(child)
    removeConsumed(state.view)
