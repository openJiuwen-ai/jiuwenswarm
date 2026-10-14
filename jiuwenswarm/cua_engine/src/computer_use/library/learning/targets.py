"""Unverified locator hints from recorded native nodes and Python call syntax."""
import ast
import re


def _queryName(node, all):
    label = next((n for n in all if n.get("ref") == node["titleRef"]), {}) if node.get("titleRef") else {}
    return node.get("name") or node.get("description") or label.get("name") or label.get("value")


def _meaningful(node, all):
    name = _queryName(node, all)
    mutable = node["role"] in ("comboBox", "textField", "textArea") and name == node.get("value")
    return name if name and not mutable and not re.match(r"^(?:file:|/)", name) else None


def _segment(node, all):
    name = _meaningful(node, all)
    return dict(role=node["role"], **({"name": name} if name else {}))


def _matches(node, match, all):
    return node["role"] == match["role"] and ("name" not in match or _queryName(node, all) == match["name"])


def _references(code):
    try: source = ast.parse(code)
    except (SyntaxError, TypeError): return []
    refs = {}
    for node in ast.walk(source):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "element" and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and re.fullmatch(r"[0-9]+", arg.value): refs[arg.value] = None
    return list(refs)


def targetHints(events):
    seen, result = {}, {}
    for event in events:
        if event.get("phase") != "task": continue
        if event.get("kind") == "connection": seen.clear()
        observation = event.get("observation", {})
        if event.get("kind") == "native_observation" and observation.get("root"):
            all, paths = [], []
            def walk(node, ancestors, window=None):
                if node["role"] == "window": window = node.get("name")
                all.append(node); paths.append(dict(node=node, ancestors=ancestors, window=window))
                for child in node.get("children", []): walk(child, [*ancestors, node], window)
            walk(observation["root"], [])
            for item in paths:
                seen[item["node"]["ref"]] = dict(**item, all=all, **({"document": observation["document"]} if "document" in observation else {}), observation=event.get("observed", {}).get("id", "unknown"))
        if event.get("kind") != "execution_start": continue
        hints = []
        for ref in _references(event.get("code")):
            item = seen.get(ref)
            if item is None: continue
            node, all, ancestors = item["node"], item["all"], item["ancestors"]
            leaf = _segment(node, all); leafMatches = sum(_matches(n, leaf, all) for n in all)
            parent = next((n for n in reversed(ancestors) if n["role"] not in ("window", "application") and _meaningful(n, all)
                and sum(_matches(other, _segment(n, all), all) for other in all) == 1), None)
            menu = next((i for i, n in enumerate(ancestors) if n["role"] == "menuBar"), -1)
            if menu >= 0: path = [dict(role="menuBar"), *(_segment(n, all) for n in ancestors[menu+1:] if n["role"] in ("menuBarItem", "menuItem")), leaf]
            elif leafMatches == 1: path = [leaf]
            elif parent is not None: path = [*(_segment(n, all) for n in ancestors[ancestors.index(parent):]), leaf]
            else: path = None
            hint = dict(ref=ref, role=node["role"], observation=item["observation"], status="unverified; resolve in the current window with find()")
            for key, value in (("displayedRole", node.get("roleDescription")), ("name", _queryName(node, all)), ("window", item.get("window"))):
                if value is not None: hint[key] = value
            if "document" in item: hint["document"] = item["document"]
            if node.get("identifier"): hint["identifierHint"] = node["identifier"]
            if path: hint["candidatePath"] = path
            hints.append(hint)
        result[event["executionId"]] = hints
    return result
