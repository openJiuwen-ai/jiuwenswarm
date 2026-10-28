"""Lossless presentation of properties with identical values and reference sets.

Each output record is a Cartesian product: every named property has exactly the
displayed value/status on every listed reference. Overlapping sets are not equal
sets. This formatter neither folds nodes nor changes the native fact graph.
"""
import json
import re


def reference_set(values):
    """Lossless shorthand for numeric display aliases, not tree order."""
    values = sorted({int(value) for value in values})
    parts, index = [], 0
    while index < len(values):
        end = index
        while end + 1 < len(values) and values[end + 1] == values[end] + 1:
            end += 1
        if end - index >= 2:
            parts.append(f"{values[index]}–{values[end]}")
        else:
            parts.extend(str(value) for value in values[index:end + 1])
        index = end + 1
    return ", ".join(parts)


def render_properties(groups):
    """Format (property, already-encoded value/status) → observed references."""
    shared = {}
    for (relation_name, value), refs in groups.items():
        key = (frozenset(refs), value)
        shared.setdefault(key, []).append(relation_name)
    lines = []
    for (refs, value), properties in shared.items():
        if not refs:
            continue
        if len(properties) == 1:
            relation_name = properties[0]
            name = relation_name if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]*', relation_name) else json.dumps(
                relation_name, ensure_ascii=False)
        else:
            name = 'Properties ' + json.dumps(properties, ensure_ascii=False, separators=(',', ':'))
        lines.append(reference_set(refs) + ' ' + name + ': ' + value)
    return lines
