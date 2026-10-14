from ..protocol.primitives import frozen
from .projection import prepareTree


def observationCoverage(observation, roots=None):
    parents, complete, displayed = {}, [], {}
    def remember(node):
        displayed[node["ref"]] = node
        for child in node["children"]:
            remember(child)
    if observation["root"] is not None:
        for root in prepareTree(observation["root"], observation["context"]) if roots is None else roots:
            remember(root)
    def visit(node, parent):
        parents[node["ref"]] = parent
        view = displayed.get(node["ref"])
        if ("childCount" in node and node.get("childrenRead") == node["childCount"] and "partialReason" not in node
                and not node.get("childOmissions") and node.get("childrenStart", 0) == 0
                and (view is None or "partialReason" not in view)):
            complete.append(node["ref"])
        for child in node["children"]:
            visit(child, node["ref"])
    if observation["root"] is not None:
        visit(observation["root"], None)
    return frozen(dict(parents=frozen(parents), complete=tuple(complete)))


def confirmsRemoval(previous, current, ref):
    if previous is None or current is None or ref in current["parents"]:
        return False
    complete = set(current["complete"])
    child = ref
    while child is not None:
        parent = previous["parents"].get(child)
        if parent is None:
            return False
        if parent in current["parents"]:
            return parent in complete
        child = parent
    return False
