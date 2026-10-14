"""Reconstruct outline hierarchy only from consistent disclosure relations."""
from .policy import sameBlocking

def outline(state, node):
    for child in node["children"]:
        outline(state, child)
    if node["role"] != "outline":
        return
    stack, children = [], []
    rows = {c["ref"]: c for c in node["children"] if c["role"] == "row"}
    acyclicResults = {}
    def acyclic(row):
        seen, valid, current = set(), True, row
        while current is not None:
            ref = current["ref"]
            if ref in acyclicResults:
                valid = acyclicResults[ref]
                break
            if ref in seen:
                valid = False
                break
            seen.add(ref)
            current = rows.get(current.get("disclosedBy", ""))
        for ref in seen:
            acyclicResults[ref] = valid
        return valid
    for row in node["children"]:
        parentIndex = next((i for i, parent in enumerate(stack) if parent["ref"] == row.get("disclosedBy")), -1)
        parent = stack[parentIndex] if parentIndex >= 0 else None
        compatibleLevel = parent is None or "disclosureLevel" not in parent or "disclosureLevel" not in row or row["disclosureLevel"] == parent["disclosureLevel"] + 1
        if (row["role"] == "row" and parent is not None and acyclic(row) and not row.get("incompleteDisclosureRelation") and compatibleLevel
                and parent.get("expanded") is not False and parent.get("disclosing") is not False and sameBlocking(row, parent)
                and parent.get("outlineDepth", 0) < 128):
            parent["children"].append(row)
            row["outlineDepth"] = parent.get("outlineDepth", 0) + 1
            state.changes.append(dict(sourceRef=row["ref"], kind="outline", targetRef=parent["ref"]))
            stack = stack[:parentIndex + 1]
        else:
            children.append(row)
            stack = []
            if row["role"] == "row":
                row["outlineDepth"] = 0
        if row["role"] == "row":
            stack.append(row)
    node["children"] = children
