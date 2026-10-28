"""Reconstruct outline hierarchy only from consistent disclosure relations."""
from .policy import same_blocking


def outline(state, node):
    for child in node["children"]:
        outline(state, child)
    if node["role"] != "outline":
        return
    stack, children = [], []
    rows = {c["ref"]: c for c in node["children"] if c["role"] == "row"}
    acyclic_results = {}

    def acyclic(row):
        seen, valid, current = set(), True, row
        while current is not None:
            ref = current["ref"]
            if ref in acyclic_results:
                valid = acyclic_results[ref]
                break
            if ref in seen:
                valid = False
                break
            seen.add(ref)
            current = rows.get(current.get("disclosedBy", ""))
        for ref in seen:
            acyclic_results[ref] = valid
        return valid

    for row in node["children"]:
        parent_index = next((i for i, parent in enumerate(stack) if parent["ref"] == row.get("disclosedBy")), -1)
        parent = stack[parent_index] if parent_index >= 0 else None
        compatible_level = parent is None or "disclosureLevel" not in parent or "disclosureLevel" not in row or row[
            "disclosureLevel"] == parent["disclosureLevel"] + 1
        confirmed_outline_parent = (row["role"] == "row" and parent is not None and acyclic(row)
                                    and not row.get("incompleteDisclosureRelation") and compatible_level
                                    and parent.get("expanded") is not False and parent.get("disclosing") is not False
                                    and same_blocking(row, parent) and parent.get("outlineDepth", 0) < 128)
        if confirmed_outline_parent:
            parent["children"].append(row)
            row["outlineDepth"] = parent.get("outlineDepth", 0) + 1
            state.changes.append(dict(sourceRef=row["ref"], kind="outline", targetRef=parent["ref"]))
            stack = stack[:parent_index + 1]
        else:
            children.append(row)
            stack = []
            if row["role"] == "row":
                row["outlineDepth"] = 0
        if row["role"] == "row":
            stack.append(row)
    node["children"] = children
