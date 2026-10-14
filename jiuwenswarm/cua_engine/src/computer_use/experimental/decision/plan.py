from ..._json import dumps


def _inputData(action):
    data = {key: value for key, value in action.items() if key not in ("target", "ref", "x", "y", "fromX", "fromY", "toX", "toY")}
    if action["op"] == "click": data.setdefault("button", "left"); data.setdefault("count", 1)
    if action["op"] == "typeText": data.setdefault("replace", False)
    if action["op"] == "scroll": data.setdefault("pages", 1)
    return dumps(sorted(data.items()))


def assertPlannedCommand(plan, command):
    if "actions" in command:
        if len(command["actions"]) > len(plan) or any(_inputData(action) != _inputData(plan[index]) for index, action in enumerate(command["actions"])):
            raise ValueError("The resolved action sequence must preserve the planned operations, order and input data. No planned input was dispatched.")
    elif command.get("op") == "done":
        raise ValueError("This stage prescribes input, not a completion report. Resolve the planned targets, read missing evidence, or hand off. No planned input was dispatched.")
