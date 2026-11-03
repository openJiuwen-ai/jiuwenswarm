"""Reversible chain layout; never combines native action targets."""
import re

from ..protocol.primitives import utf16_length
from .tree import has_display_value, has_incomplete_children

_NODE = re.compile(r"^(\t*)([0-9]+) (.+)\Z")
_STATES = ('disabled|reported disabled|selected|selectable|clickable|text-input|repl'
           'ace-only|replace unavailable|replace unknown|submenu|focusable|expanded|'
           'collapsed|focused|reports focus|modal|non-modal|contains selected '
           'text')
_MEMBER = re.compile(r"^([0-9]+)(?: \((" + _STATES + r")(?:, (?:" + _STATES + r"))*\))?\Z")


def group_body(body):
    match = re.match(r"^container(?: \(([^)]+)\))?(.*)\Z", body)
    return dict(states=match[1] or "", common="container" + match[2]) if match else None


def compact_tree_chains(lines, roots):
    nodes = {}

    def collect(node):
        nodes[node["ref"]] = node
        for child in node["children"]:
            collect(child)

    for root in roots:
        collect(root)

    def anonymous(node):
        return node["role"] == "group" and not any(
            node.get(k)
            for k in ("name", "description", "identifier", "help", "url", "preserveGroup", "modal", "subrole"))

    def joinable(parent, child, parent_body, child_body):
        incomplete_single_child = len(
            parent["children"]) != 1 or parent["children"][0] is not child or has_incomplete_children(
                parent) or has_incomplete_children(child)
        if incomplete_single_child:
            return False
        a, b = group_body(parent_body), group_body(child_body)
        same_anonymous_context = anonymous(parent) and anonymous(
            child) and a is not None and b is not None and a["common"] == b["common"]
        if same_anonymous_context:
            return True
        redundant_fact_groups = (parent.get('_factBacked') and child.get('_factBacked')
                                 and parent['role'] == child['role'] == 'group' and not any(
                                     node.get(k) for node in (parent, child)
                                     for k in ('preserveGroup', 'modal', 'subrole', 'blockedBy')))
        if redundant_fact_groups:
            # A named single-child wrapper chain is still a chain. Keep every
            # name, state, source and action verbatim, using reversible `>`
            # layout instead of spending one indentation level on each name.
            return True
        anonymous_control_wrapper = (anonymous(parent) and not has_display_value(parent) and "control" not in parent
                                     and not parent.get("titleRef") and not parent.get("labelTargets")
                                     and not parent.get("incompleteLabelRelation") and not parent.get("blockedBy") and
                                     child["role"] in ("button", "popUpButton", "menuButton", "checkBox", "radioButton",
                                                       "switch", "link", "textField", "textArea", "comboBox")
                                     and not child.get("modal") and not child.get("subrole")
                                     and not child.get("blockedBy"))
        if anonymous_control_wrapper:
            return True
        return ((parent["role"] == "row" and child["role"] in ("cell", "staticText", "textField"))
                or (parent["role"] == "cell" and child["role"] in ("staticText", "textField")))

    output, shifts, i = [], [], 0
    while i < len(lines):
        first = _NODE.match(lines[i])
        boundary = first or re.match(r"^(\t*)Empty values \[", lines[i])
        if boundary:
            while shifts and len(boundary[1]) <= shifts[-1]["endDepth"]:
                shifts.pop()
        shift = sum(item["count"] for item in shifts)
        entries = [first] if first else []
        text = first[2] + " " + first[3] if first else ""
        while entries:
            previous = entries[-1]
            next_match = _NODE.match(lines[i + len(entries)]) if i + len(entries) < len(lines) else None
            parent, child = nodes.get(previous[2]), nodes.get(next_match[2]) if next_match else None
            ends_chain = (next_match is None or parent is None or child is None
                          or len(next_match[1]) != len(previous[1]) + 1
                          or not joinable(parent, child, previous[3], next_match[3]))
            if ends_chain:
                break
            candidate = entries + [next_match]
            groups = [group_body(e[3]) for e in candidate]
            common = (all(anonymous(nodes[e[2]]) for e in candidate)
                      and all(g is not None and groups[0] is not None and g["common"] == groups[0]["common"]
                              for g in groups))
            already_shared_group = (not common and len(entries) > 1 and all(anonymous(nodes[e[2]]) for e in entries) and
                                    all(g is not None and groups[0] is not None and g["common"] == groups[0]["common"]
                                        for g in groups[:-1]))
            if already_shared_group:
                break
            if common:
                joined = " > ".join(e[2] + " " + e[3] if index == len(candidate) - 1 else e[2] +
                                    (" (" + groups[index]["states"] + ")" if groups[index]["states"] else "")
                                    for index, e in enumerate(candidate))
            else:
                joined = " > ".join(e[2] + " " + e[3] for e in candidate)
            if utf16_length(joined) > 320:
                break
            entries.append(next_match)
            text = joined
        if len(entries) > 1:
            output.append("\t" * (len(first[1]) - shift) + text)
            shifts.append(dict(endDepth=len(entries[-1][1]), count=len(entries) - 1))
            i += len(entries)
        else:
            line = lines[i]
            i += 1
            indent = len(line) - len(line.lstrip("\t"))
            output.append(line[min(indent, shift):])
    return output


def split_chain(text):
    parts, quoted, escaped, start, i = [], False, False, 0, 0
    while i < len(text):
        char = text[i]
        if escaped:
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted and text.startswith(" > ", i):
            parts.append(text[start:i])
            start = i + 3
            i += 2
        i += 1
    parts.append(text[start:])
    return parts


def expand_tree_chains(lines):
    output, shifts = [], []
    for line in lines:
        first = _NODE.match(line)
        compact = re.match(r"^(\t*)([0-9]+ > .+)\Z", line)
        boundary = first or compact or re.match(r"^(\t*)Empty values \[", line)
        if boundary:
            indent = len(boundary[1])
            while shifts and indent <= shifts[-1]["depth"]:
                shifts.pop()
            shift = sum(item["count"] for item in shifts)
            parts = split_chain(line[indent:])
            last = re.match(r"^([0-9]+) (.+)\Z", parts[-1])
            if len(parts) > 1 and last and all(re.match(r"^(?:0|[1-9][0-9]*)(?: .+)?\Z", part) for part in parts):
                for index, part in enumerate(parts):
                    shared, group = _MEMBER.match(part), group_body(last[2])
                    match = re.match(r"^[0-9]+ (\(.+\))\Z", part) if shared else None
                    states = match[1] if match else None
                    body = (shared[1] + " container" + (" " + states if states else "") +
                            group["common"][len("container"):] if shared and group else part)
                    output.append("\t" * (indent + shift + index) + body)
                shifts.append(dict(depth=indent, count=len(parts) - 1))
            else:
                output.append("\t" * shift + line)
        else:
            shift = sum(item["count"] for item in shifts)
            output.append("\t" * shift + line if line.startswith("\t") else line)
    return output
