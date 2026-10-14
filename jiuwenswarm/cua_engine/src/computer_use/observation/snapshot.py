"""Diffs are presentation only; raw native snapshots always remain full."""
import re
from ..protocol.primitives import reference, trim, trimStart, utf16Length
from .render import field
from .chains import expandTreeChains
from .text_parts import expandTextParts


def parse(text, refs):
    header, footer, nodes, stack, seen, expanded = [], [], [], [], set(), []
    for line in expandTreeChains(expandTextParts(text.split("\n"), field)):
        cells = re.match(r'^(\t*)Empty values \[(.+); ((?:(?:Click|Input): (?:available|unavailable|unknown), )*Value: "")\]: ((?:[0-9]+ [A-Z]+[1-9][0-9]*)(?:, [0-9]+ [A-Z]+[1-9][0-9]*){2,})\Z', line)
        if not cells:
            expanded.append(line)
        else:
            for pair in cells[4].split(", "):
                ref, address = pair.split(" ")
                expanded.append(f"{cells[1]}{ref} {cells[2]} {address}, {cells[3]}")
    for line in expanded:
        if re.match(r"^(?:Windows: |Window documents: |Window relations: |Interaction: |Window: |Observation: |Document: |App: |Application menu: |Tree: |Screenshot: |Overview: |Overview windows |Overview bounds: |Window title omitted: )", line):
            if nodes:
                return None
            header.append(line); continue
        if re.match(r"^(?:Selected: |Selected menus: |The focused UI element is |Reported focus: |Focus flags: |Shared focus: |Multiple elements report focus: |Tree partial: |Other windows \(not expanded\): |\[)", line):
            footer.append(line); continue
        match = re.match(r"^(\t*)([^\s]+) ", line)
        if match and reference(match[2]):
            id, depth = match[2], len(match[1])
            if id not in refs or id in seen or depth > len(stack) or footer:
                return None
            nodes.append(dict(id=id, parent=stack[depth - 1] if depth else None, depth=depth, text=line))
            seen.add(id)
            stack = stack[:depth] + [id]
        elif not nodes:
            header.append(line)
        elif re.match(r"^\t+(?:Value:|Selected text:|\| |Omitted: |Children: |Continue: |Inspect subtree: )", line) and not footer:
            nodes[-1]["text"] += "\n" + line
        else:
            footer.append(line)
    if not nodes or len(nodes) != len(refs):
        return None
    return dict(header="\n".join(header), footer=trim("\n".join(footer)), nodes=nodes)


def header(text, mode):
    title = f'Tree: {mode} (not full; read({{"full": True}}) for full text)'
    if re.search(r"^Tree: ", text, re.MULTILINE):
        return re.sub(r"^Tree: .*$", lambda _: title, text, count=1, flags=re.MULTILINE)
    lines = text.split("\n")
    lines.insert(1, title)
    return "\n".join(lines)


def firstLine(node): return node["text"].split("\n", 1)[0]


def snapshotDiff(previous, current, notObserved=None):
    # A display number may be reused by the next full observation. Never call
    # a newly bound object "unchanged" merely because its label looks the same.
    if any(previous['refs'][ref] != current['refs'][ref]
           for ref in previous['refs'].keys() & current['refs'].keys()):
        return current['text']
    notObserved = set() if notObserved is None else set(notObserved)
    before, after = parse(previous["text"], previous["refs"]), parse(current["text"], current["refs"])
    def appHeader(value):
        line = next((line for line in value.split("\n") if re.match(r"^(?:Window: |App: )", line)), None)
        return re.sub(r'^Window: "(?:[^"\\]|\\.)*", ', "", line, count=1) if line is not None else None
    if before is None or after is None or appHeader(before["header"]) != appHeader(after["header"]):
        return current["text"]
    def currentWindow(value):
        match = re.search(r"^Windows: .*; current=([0-9]+|unknown)$", value, re.MULTILINE)
        return match[1] if match else None
    if currentWindow(before["header"]) != currentWindow(after["header"]):
        return current["text"]
    def document(value): return next((line for line in value.split("\n") if line.startswith("Document: ")), None)
    if document(before["header"]) != document(after["header"]):
        return current["text"]
    def interaction(value): return "\n".join(line for line in value.split("\n") if re.match(r"^(?:Interaction: |Window relations: |Application menu: )", line))
    if interaction(before["header"]) != interaction(after["header"]):
        return current["text"]
    old, next_ = {n["id"]: n for n in before["nodes"]}, {n["id"]: n for n in after["nodes"]}
    if ([n["id"] for n in before["nodes"] if n["id"] in next_] != [n["id"] for n in after["nodes"] if n["id"] in old]
            or any(n["id"] in old and old[n["id"]]["parent"] != n["parent"] for n in after["nodes"])):
        return current["text"]
    missing = [n["id"] for n in before["nodes"] if n["id"] not in next_]
    removed, unobserved = [id for id in missing if id not in notObserved], [id for id in missing if id in notObserved]
    changes = {n["id"]: "~" if n["id"] in old else "+" for n in after["nodes"] if old.get(n["id"], {}).get("text") != n["text"]}
    context, expanded, children = {}, set(), {}
    for node in after["nodes"]:
        if node["parent"]:
            children.setdefault(node["parent"], []).append(node)
    def expand(id):
        for child in children.get(id, []):
            context[child["id"]] = None
            expanded.add(child["id"])
            expand(child["id"])
    states = "disabled|selected|selectable|clickable|submenu|focusable|expanded|collapsed|focused|reports focus|modal|non-modal|contains selected text"
    def withoutStates(node): return re.sub(r" \((?:" + states + r")(?:, (?:" + states + r"))*\)", "", firstLine(node), count=1)
    for id in changes:
        if id in old and next_[id]["parent"] and id in children and withoutStates(old[id]) != withoutStates(next_[id]):
            expand(id)
    for node in after["nodes"]:
        if (re.search(r"^\t+Children: (?:showing [0-9]+ of [0-9]+ items; )?partial: ", node["text"], re.MULTILINE)
                or re.search(r" \((?:showing [0-9]+ of [0-9]+ items; )?partial: .+\)\Z", firstLine(node))):
            context[node["id"]] = None
    for id in list(changes) + list(context):
        parent = next_.get(id, {}).get("parent")
        while parent:
            context[parent] = None
            parent = next_.get(parent, {}).get("parent")
    for id in missing:
        parent = old.get(id, {}).get("parent")
        while parent:
            if parent in next_:
                context[parent] = None
            parent = old.get(parent, {}).get("parent")
    pending, visited, i = list(changes) + list(context), set(), 0
    while i < len(pending):
        id = pending[i]; i += 1
        if id in visited:
            continue
        visited.add(id)
        for related in current.get("contextRefs", {}).get(id, []):
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
        id = node["id"]
        if id in changes:
            lines.append(changes[id] + node["text"])
        elif id in context:
            lines.append(" " + (node["text"] if id in expanded else firstLine(node)))
        if id not in changes:
            for line in node["text"].split("\n")[1:]:
                if re.match(r"^\t+Omitted: ", line):
                    lines.append(f"Element {id}: {trimStart(line)}")
                if re.match(r"^\t+(?:Children: |Continue: |Inspect subtree: )", line) and id not in expanded:
                    lines.append(line)
    if after["footer"]:
        lines.append(after["footer"])
    diff = "\n".join(lines)
    # Only the executable hint changes language. Keep the established full/diff
    # boundary: quoting a Python dict must not change which evidence is returned.
    hintGrowth = len('read({"full": True})') - len('read({full:true})')
    return diff if utf16Length(diff) - hintGrowth < utf16Length(current["text"]) else current["text"]
