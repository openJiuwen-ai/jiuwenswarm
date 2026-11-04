import json
import re

import regex
from computer_use._json import loads as jsonLoads

from ..protocol.primitives import utf16_length
from .formatting import json_string
from .presentation_policy import editing
from .tree import has_incomplete_children

_GENERIC = {"AXShowMenu", "AXScrollToVisible"}


TEXT_LABEL_FIELDS = (
    "name",
    "description",
    "identifier",
    "help",
    "url",
    "placeholder",
    "focused",
    "focusable",
    "selected",
    "selectable",
    "pressable",
)
TEXT_STATE_FIELDS = (
    "selectedText",
    "valueExpected",
    "valueIsNumber",
    "titleRef",
    "labelTargets",
    "incompleteLabelRelation",
    "linkedLabel",
    "subrole",
    "modal",
    "ownedModals",
    "blockedBy",
    "preserveGroup",
    "expandable",
    "expanded",
    "disclosing",
    "submenu",
)


def compact_text_parts(lines, roots, focused_ref, formatter):
    candidates = {}

    def collect(parent, in_table=False):
        in_table = in_table or parent["role"] in ("table", "row", "cell", "column", "outline", "list")
        # Fact-backed projection has already proved which wrappers are purely
        # structural. Pack the resulting siblings, even if they had separate
        # transparent parents. Do not cross any retained container or control.
        siblings = parent['children'] if parent.get('_factBacked') else parent['native']['children']
        positions = {child["ref"]: index for index, child in enumerate(siblings)}
        for node in parent["children"]:
            index = positions.get(node["ref"])
            value = node.get("value")
            container = parent['role'] == 'group' or (parent.get('_factBacked')
                                                      and parent['role'] in ('webArea', 'scrollArea'))
            can_group_text = (
                index is not None and not in_table and container and "control" not in parent
                and not has_incomplete_children(parent) and not parent.get('preserveGroup')
                and not parent.get('subrole') and not editing(parent) and not parent.get('selectedText')
                and not parent.get('selected') and parent['ref'] != focused_ref and not parent.get('focused')
                and node["role"] == "staticText" and value and utf16_length(value) <= 160
                and not re.search(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", value) and not node["children"]
                and not has_incomplete_children(node) and node["ref"] != focused_ref and not any(
                    node.get(k) for k in TEXT_LABEL_FIELDS)
                and node.get("enabled") is not False and "selectionRange" not in node and not any(
                    node.get(k)
                    for k in TEXT_STATE_FIELDS)
                and "control" not in node and "disclosureLevel" not in node and not node.get("disclosedBy")
                and not node.get("incompleteDisclosureRelation") and "shortcut" not in node and "menuMark" not in node
                and not node.get("actionsUnavailable") and not any(a["name"] not in _GENERIC
                                                                   for a in node.get("actions", []))
                and "available" not in node.get("capabilities", {}).values())
            if can_group_text:
                candidates[node["ref"]] = dict(parent=parent, index=index, node=node)
            collect(node, in_table)

    for root in roots:
        collect(root)

    def part(line):
        match = regex.match(r"^(\t*)([0-9]+) ([\p{L}\p{N} _-]+) Value: (.+)\Z", line)
        if match is None or match[2] not in candidates:
            return None
        source = candidates[match[2]]
        value, body = formatter(source["node"]["value"]), match[4]
        if not body.startswith(value):
            return None
        suffix = body[len(value):]
        if (suffix and not suffix.startswith(", Secondary Actions: ")) or re.search(r"[\[\]\r\n]", suffix):
            return None
        facts = json.dumps([source["node"].get("capabilities"), source["node"].get("actions")], ensure_ascii=True,
                           separators=(",", ":"))
        return {
            **source, "indent": match[1], "ref": match[2], "properties":
            match[3] + ("; " + suffix[2:] if suffix else ""), "facts": facts
        }

    output, i = [], 0
    while i < len(lines):
        first = part(lines[i])
        if first is None:
            output.append(lines[i])
            i += 1
            continue
        run = [first]

        def line():
            return first["indent"] + "Text parts [" + first["properties"] + "]: " + \
                ", ".join(item["ref"] + " " + json_string(item["node"]["value"]) for item in run)

        while i + len(run) < len(lines):
            next_part = part(lines[i + len(run)])
            ends_text_run = (next_part is None or next_part["parent"] is not first["parent"]
                             or next_part["index"] != run[-1]["index"] + 1 or next_part["indent"] != first["indent"]
                             or next_part["properties"] != first["properties"] or next_part["facts"] != first["facts"]
                             or len(run) >= 8)
            if ends_text_run:
                break
            run.append(next_part)
            if utf16_length(line()) > 480:
                run.pop()
                break
        packed = line()
        if len(run) >= 2 and utf16_length(packed) < utf16_length("\n".join(lines[i:i + len(run)])):
            output.append(packed)
            i += len(run)
        else:
            output.append(lines[i])
            i += 1
    return output


def expand_text_parts(lines, formatter):
    output = []
    for line in lines:
        match = regex.match(r"^(\t*)Text parts \[([\p{L}\p{N} _-]+)(?:; ([^\[\]\r\n]+))?\]: (.+)\Z", line)
        if not match:
            output.append(line)
            continue
        entries = list(re.finditer(r'(0|[1-9][0-9]*) ("(?:[^"\\]|\\.)*")', match[4]))
        if len(entries) < 2 or ", ".join(e[0] for e in entries) != match[4]:
            output.append(line)
            continue
        try:
            expanded = [
                f"{match[1]}{e[1]} {match[2]} Value: {formatter(jsonLoads(e[2]))}"
                + (", " + match[3] if match[3] else "")
                for e in entries
            ]
        except (ValueError, TypeError):
            output.append(line)
        else:
            output.extend(expanded)
    return output
