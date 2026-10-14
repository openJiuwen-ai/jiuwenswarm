"""Pack empty table values without merging cell identities or coordinates."""
import re
from ..protocol.primitives import safeInteger


def compactEmptyValues(lines, roots, focusedRef=None):
    parents = {}
    def collect(node, parent=None, inTable=False):
        inTable = inTable or node["role"] == "table"
        if (inTable and parent is not None and parent["role"] in ("table", "row", "column") and node["role"] in ("cell", "textField") and node.get("value") == "" and not node["children"]
                and node["ref"] != focusedRef and not node.get("focused") and not node.get("selected") and "selectionRange" not in node and not node.get("selectedText") and "partialReason" not in node
                and not node.get("labelTargets") and not node.get("incompleteLabelRelation") and not node.get("blockedBy")):
            parents[node["ref"]] = parent["ref"]
        for child in node["children"]:
            collect(child, node, inTable)
    for root in roots:
        collect(root)
    def cell(line):
        match = re.match(r'^(\t*)([0-9]+) (.+) ([A-Z]+)([1-9][0-9]*), ((?:(?:Click|Input): (?:available|unavailable|unknown), )*)Value: ""\Z', line)
        if match is None or match[2] not in parents:
            return None
        indent, ref, properties, letters, rowText, fields = match.groups()
        column = 0
        for char in letters:
            column = column * 26 + ord(char) - 64
        row = int(rowText)
        if not safeInteger(column) or not safeInteger(row):
            return None
        return dict(ref=ref, address=letters + rowText, parent=parents[ref], indent=indent, properties=properties, fields=fields, column=column, row=row)
    output, index = [], 0
    while index < len(lines):
        first = cell(lines[index])
        if first is None:
            output.append(lines[index]); index += 1; continue
        run, direction = [first], None
        while index + len(run) < len(lines):
            next_cell = cell(lines[index + len(run)])
            if next_cell is None or any(next_cell[k] != first[k] for k in ("parent", "indent", "properties", "fields")):
                break
            previous = run[-1]
            horizontal = next_cell["row"] == previous["row"] and next_cell["column"] - previous["column"] == 1
            vertical = next_cell["column"] == previous["column"] and next_cell["row"] - previous["row"] == 1
            if not ((horizontal or vertical) if direction is None else horizontal if direction else vertical):
                break
            if direction is None:
                direction = horizontal
            run.append(next_cell)
        if len(run) >= 3:
            output.append(f"{first['indent']}Empty values [{first['properties']}; {first['fields']}Value: \"\"]: " + ", ".join(c["ref"] + " " + c["address"] for c in run))
            index += len(run)
        else:
            output.append(lines[index]); index += 1
    return output
