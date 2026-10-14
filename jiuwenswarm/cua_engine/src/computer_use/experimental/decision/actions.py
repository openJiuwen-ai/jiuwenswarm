from ..._identity import Record
from ..._json import dumps
from ...errors import ComputerUseError


def bindActions(app, actions, refs, imageShown):
    bound = []
    for action in actions:
        def fail(detail):
            raise ComputerUseError(dict(code="invalid_request", effect="none", target=dumps(action), detail=detail, next="Read the current app and select an observed target."))
        if "ref" in action and action["ref"] not in refs: fail("Element reference is not in the current observation.")
        if ("x" in action or action["op"] == "drag") and not imageShown: fail("Coordinate input requires the current window screenshot; request read with image:true first.")
        element = app.element(action["ref"]) if "ref" in action else None
        async def run(action=action, element=element):
            op = action["op"]
            if op == "click":
                options = {key: action[key] for key in ("button", "count") if key in action}
                return await (element.click(options) if element is not None else app.click(action["x"], action["y"], options))
            if op == "typeText": return await element.typeText(action["text"], dict(replace=action.get("replace", False)))
            if op == "paste": return await (element if element is not None else app).paste(action["text"])
            if op == "pressKey": return await (element if element is not None else app).pressKey(action["key"])
            if op == "openMenu": return await element.openMenu()
            if op == "scroll":
                return await (element.scroll(action["direction"], action.get("pages", 1)) if element is not None else app.scroll(action["x"], action["y"], action["direction"], action.get("pages", 1)))
            if op == "drag": return await app.drag({key: action[key] for key in ("fromX", "fromY", "toX", "toY")})
            raise ValueError("Unknown decision action")
        bound.append(Record(action=action, run=run))
    return bound
