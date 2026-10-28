"""Diffs are presentation only; raw native snapshots always remain full."""
import re

from ..protocol.primitives import reference, trim, trim_start, utf16_length
from .chains import expand_tree_chains
from .render import field
from .text_parts import expand_text_parts


def parse(text, refs):
    header_lines, footer, nodes, stack, seen, expanded = [], [], [], [], set(), []
    for line in expand_tree_chains(expand_text_parts(text.split("\n"), field)):
        cells = re.match(('^(\\t*)Empty values \\[(.+); ((?:(?:Click|Input): '
                          '(?:available|unavailable|unknown), )*Value: "")\\]: ((?:[0-9]+ '
                          '[A-Z]+[1-9][0-9]*)(?:, [0-9]+ '
                          '[A-Z]+[1-9][0-9]*){2,})\\Z'), line)
        if not cells:
            expanded.append(line)
        else:
            for pair in cells[4].split(", "):
                ref, address = pair.split(" ")
                expanded.append(f"{cells[1]}{ref} {cells[2]} {address}, {cells[3]}")
    for line in expanded:
        if re.match(('^(?:Windows: |Window documents: |Window relations: |Interaction: '
                     '|Window: |Observation: |Document: |App: |Application menu: |Tree: '
                     '|Screenshot: |Overview: |Overview windows |Overview bounds: |Window '
                     'title omitted: '
                     ')'), line):
            if nodes:
                return None
            header_lines.append(line)
            continue
        if re.match(('^(?:Selected: |Selected menus: |The focused UI element is |Reported '
                     'focus: |Focus flags: |Shared focus: |Multiple elements report focus: '
                     '|Tree partial: |Other windows \\(not expanded\\): '
                     '|\\[)'), line):
            footer.append(line)
            continue
        match = re.match(r"^(\t*)([^\s]+) ", line)
        if match and reference(match[2]):
            element_id, depth = match[2], len(match[1])
            invalid_tree_reference = element_id not in refs or element_id in seen or depth > len(stack) or footer
            if invalid_tree_reference:
                return None
            nodes.append(dict(id=element_id, parent=stack[depth - 1] if depth else None, depth=depth, text=line))
            seen.add(element_id)
            stack = stack[:depth] + [element_id]
        elif not nodes:
            header_lines.append(line)
        elif re.match(r"^\t+(?:Value:|Selected text:|\| |Omitted: |Children: |Continue: |Inspect subtree: )",
                      line) and not footer:
            nodes[-1]["text"] += "\n" + line
        else:
            footer.append(line)
    if not nodes or len(nodes) != len(refs):
        return None
    return dict(header="\n".join(header_lines), footer=trim("\n".join(footer)), nodes=nodes)


def header(text, mode):
    title = f'Tree: {mode} (not full; read({{"full": True}}) for full text)'
    if re.search(r"^Tree: ", text, re.MULTILINE):
        return re.sub(r"^Tree: .*$", lambda _: title, text, count=1, flags=re.MULTILINE)
    lines = text.split("\n")
    lines.insert(1, title)
    return "\n".join(lines)


def first_line(node):
    return node["text"].split("\n", 1)[0]


def snapshot_diff(previous, current, not_observed=None):
    # A display number may be reused by the next full observation. Never call
    # a newly bound object "unchanged" merely because its label looks the same.
    if any(previous['refs'][ref] != current['refs'][ref] for ref in previous['refs'].keys() & current['refs'].keys()):
        return current['text']
    not_observed = set() if not_observed is None else set(not_observed)
    before, after = parse(previous["text"], previous["refs"]), parse(current["text"], current["refs"])

    def app_header(value):
        line = next((line for line in value.split("\n") if re.match(r"^(?:Window: |App: )", line)), None)
        return re.sub(r'^Window: "(?:[^"\\]|\\.)*", ', "", line, count=1) if line is not None else None

    if before is None or after is None or app_header(before["header"]) != app_header(after["header"]):
        return current["text"]

    def current_window(value):
        match = re.search(r"^Windows: .*; current=([0-9]+|unknown)$", value, re.MULTILINE)
        return match[1] if match else None

    if current_window(before["header"]) != current_window(after["header"]):
        return current["text"]

    def document(value):
        return next((line for line in value.split("\n") if line.startswith("Document: ")), None)

    if document(before["header"]) != document(after["header"]):
        return current["text"]

    def interaction(value):
        return "\n".join(line for line in value.split("\n")
                         if re.match(r"^(?:Interaction: |Window relations: |Application menu: )", line))

    if interaction(before["header"]) != interaction(after["header"]):
        return current["text"]
    old, next_ = {n["id"]: n for n in before["nodes"]}, {n["id"]: n for n in after["nodes"]}
    if ([n["id"] for n in before["nodes"] if n["id"] in next_] != [n["id"] for n in after["nodes"] if n["id"] in old]
            or any(n["id"] in old and old[n["id"]]["parent"] != n["parent"] for n in after["nodes"])):
        return current["text"]
    missing = [n["id"] for n in before["nodes"] if n["id"] not in next_]
    removed, unobserved = (
        [element_id for element_id in missing if element_id not in not_observed],
        [element_id for element_id in missing if element_id in not_observed],
    )
    changes = {
        n["id"]: "~" if n["id"] in old else "+"
        for n in after["nodes"] if old.get(n["id"], {}).get("text") != n["text"]
    }
    context, expanded, children = {}, set(), {}
    for node in after["nodes"]:
        if node["parent"]:
            children.setdefault(node["parent"], []).append(node)

    def expand(element_id):
        for child in children.get(element_id, []):
            context[child["id"]] = None
            expanded.add(child["id"])
            expand(child["id"])

    states = ('disabled|selected|selectable|clickable|submenu|focusable|expanded|collap'
              'sed|focused|reports focus|modal|non-modal|contains selected '
              'text')

    def without_states(node):
        return re.sub(r" \((?:" + states + r")(?:, (?:" + states + r"))*\)", "", first_line(node), count=1)

    for element_id in changes:
        changed_parent_context = (
            element_id in old
            and next_[element_id]["parent"]
            and element_id in children
            and without_states(old[element_id]) != without_states(next_[element_id])
        )
        if changed_parent_context:
            expand(element_id)
    for node in after["nodes"]:
        if (re.search(r"^\t+Children: (?:showing [0-9]+ of [0-9]+ items; )?partial: ", node["text"], re.MULTILINE)
                or re.search(r" \((?:showing [0-9]+ of [0-9]+ items; )?partial: .+\)\Z", first_line(node))):
            context[node["id"]] = None
    for element_id in list(changes) + list(context):
        parent = next_.get(element_id, {}).get("parent")
        while parent:
            context[parent] = None
            parent = next_.get(parent, {}).get("parent")
    for element_id in missing:
        parent = old.get(element_id, {}).get("parent")
        while parent:
            if parent in next_:
                context[parent] = None
            parent = old.get(parent, {}).get("parent")
    pending, visited, i = list(changes) + list(context), set(), 0
    while i < len(pending):
        element_id = pending[i]
        i += 1
        if element_id in visited:
            continue
        visited.add(element_id)
        for related in current.get("contextRefs", {}).get(element_id, []):
            if related not in next_:
                continue
            ref = related
            while ref:
                if ref not in context:
                    context[ref] = None
                    pending.append(ref)
                ref = next_.get(ref, {}).get("parent")
    mode = "diff" if missing or changes else "unchanged"
    lines = [header(after["header"], mode)]
    if removed:
        lines.append("Removed element IDs: " + ", ".join(removed))
    if unobserved:
        lines.append("Not observed in this read (not proof of removal): " + ", ".join(unobserved))
    for node in after["nodes"]:
        element_id = node["id"]
        if element_id in changes:
            lines.append(changes[element_id] + node["text"])
        elif element_id in context:
            lines.append(" " + (node["text"] if element_id in expanded else first_line(node)))
        if element_id not in changes:
            for line in node["text"].split("\n")[1:]:
                if re.match(r"^\t+Omitted: ", line):
                    lines.append(f"Element {element_id}: {trim_start(line)}")
                if re.match(r"^\t+(?:Children: |Continue: |Inspect subtree: )", line) and element_id not in expanded:
                    lines.append(line)
    if after["footer"]:
        lines.append(after["footer"])
    diff = "\n".join(lines)
    # Only the executable hint changes language. Keep the established full/diff
    # boundary: quoting a Python dict must not change which evidence is returned.
    hint_growth = len('read({"full": True})') - len('read({full:true})')
    return diff if utf16_length(diff) - hint_growth < utf16_length(current["text"]) else current["text"]
