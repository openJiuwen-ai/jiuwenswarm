"""Decision data contract; schemas are identical to the published v6 contract."""
from computer_use._json import loads as jsonLoads
import json
from pathlib import Path
from ...protocol.schema import Schema, SchemaError, MISSING, obj, string, number, boolean, enum, array, union, ref
from ...protocol.primitives import trim
from ...protocol.validation import validateKey, validatePasteText, validateText

decisionKey = "computer-use/decision"


class Trimmed(Schema):
    def __init__(self, **options): super().__init__("string", **options)
    def _parse(self, value, path, issues):
        return super()._parse(trim(value) if isinstance(value, str) else value, path, issues)


class Refined(Schema):
    def __init__(self, inner, refine=None, transform=None):
        super().__init__("refined"); self.inner, self.refine, self.transform = inner, refine, transform
    def _parse(self, value, path, issues):
        start = len(issues); result = self.inner._parse(value, path, issues)
        aborted = any(issue["code"] in ("invalid_type", "invalid_value", "invalid_union", "unrecognized_keys") for issue in issues[start:])
        if not aborted and self.refine is not None: self.refine(result, path, issues)
        return self.transform(result) if len(issues) == start and self.transform is not None else result


class Discriminated(Schema):
    def __init__(self, variants): super().__init__("discriminated"); self.variants = variants
    def _parse(self, value, path, issues):
        if not isinstance(value, dict): return obj({})._parse(value, path, issues)
        variant = self.variants.get(value.get("op", MISSING)) if type(value.get("op")) in (str, type(None)) or "op" not in value else None
        if variant is None:
            expected = " | ".join("'undefined'" if key is MISSING else repr(key) for key in self.variants)
            issues.append(dict(code="invalid_union", errors=[], note="No matching discriminator", discriminator="op", options=[None if key is MISSING else key for key in self.variants], path=[*path, "op"], message="Invalid discriminator value. Expected " + expected))
            return value
        return variant._parse(value, path, issues)


class Undefined(Schema):
    def __init__(self): super().__init__("undefined")
    def _parse(self, value, path, issues):
        if value is not MISSING: issues.append(dict(expected="undefined", code="invalid_type", path=path, message=f"Invalid input: expected undefined, received {'null' if value is None else type(value).__name__}"))
        return value


def checked(validate):
    def refine(value, path, issues):
        try: validate(value)
        except Exception as error: issues.append(dict(code="custom", message=str(error), path=path))
    return Refined(string(max=65536), refine)


_instruction, _app = Trimmed(min=1, max=16000), Trimmed(min=1, max=4096)
taskSchema = obj(dict(instruction=_instruction, app=_app))
_button, _count = enum("left", "right", "middle").optional(), union(enum(1), enum(2)).optional()
_coordinate, _direction = number(min=0), enum("up", "down", "left", "right")
_pages = number(min=0, exclusiveMin=True, max=20).optional()


def _pointer(value, path, issues):
    hasRef = "ref" in value
    valid = hasRef and "x" not in value and "y" not in value and _valid(ref, value["ref"]) or not hasRef and "x" in value and "y" in value and _valid(_coordinate, value["x"]) and _valid(_coordinate, value["y"])
    if not valid:
        issues.append(dict(code="custom", message="Specify either ref OR both x and y, never both target forms.", path=path))


def _valid(schema, value):
    try: schema.parse(value); return True
    except SchemaError: return False


def _pointerOrder(value):
    rest = {key: v for key, v in value.items() if key not in ("ref", "x", "y")}
    return {**rest, **({"ref": value["ref"]} if "ref" in value else dict(x=value["x"], y=value["y"]))}


def _operation(op, **fields): return obj(dict(op=enum(op), **fields))


_pointerFields = dict(ref=ref.optional(), x=_coordinate.optional(), y=_coordinate.optional())
actionSchema = Discriminated(dict(
    click=Refined(_operation("click", **_pointerFields, button=_button, count=_count), _pointer, _pointerOrder),
    typeText=_operation("typeText", ref=ref, text=checked(validateText), replace=boolean().optional()),
    paste=_operation("paste", ref=ref.optional(), text=checked(validatePasteText)),
    pressKey=_operation("pressKey", ref=ref.optional(), key=checked(validateKey)),
    openMenu=_operation("openMenu", ref=ref),
    scroll=Refined(_operation("scroll", **_pointerFields, direction=_direction, pages=_pages), _pointer, _pointerOrder),
    drag=_operation("drag", fromX=_coordinate, fromY=_coordinate, toX=_coordinate, toY=_coordinate)))
_target = Trimmed(min=1, max=4096)
intentSchema = Discriminated(dict(
    click=_operation("click", target=_target, button=_button, count=_count),
    typeText=_operation("typeText", target=_target, text=checked(validateText), replace=boolean().optional()),
    paste=_operation("paste", target=_target.optional(), text=checked(validatePasteText)),
    pressKey=_operation("pressKey", target=_target.optional(), key=checked(validateKey)),
    openMenu=_operation("openMenu", target=_target),
    scroll=_operation("scroll", target=_target, direction=_direction, pages=_pages),
    drag=_operation("drag", target=_target)))
_plannedActions = Refined(array(intentSchema, max=12), transform=lambda value: value if value else MISSING)
stageSchema = obj(dict(instruction=_instruction, actions=_plannedActions.optional()))
stageTaskSchema = obj(dict(instruction=_instruction, app=_app, actions=_plannedActions.optional()))
delegationSchema = obj(dict(instruction=_instruction, app=_app, steps=array(union(_instruction, stageSchema), min=1, max=12).optional()))


def _readChoice(value, path, issues):
    valid = ("ref" in value and "menuPath" not in value and _valid(ref, value["ref"]) and ("offset" not in value or _valid(number(integer=True, min=0), value["offset"]))) \
        or ("ref" not in value and "offset" not in value and ("menuPath" not in value or _valid(array(Trimmed(min=1, max=256), min=1, max=8), value["menuPath"])))
    if "window" in value and not _valid(ref, value["window"]): valid = False
    if not valid:
        issues.append(dict(code="custom", message="Use ref/offset OR menuPath; offset requires ref. window may select the observed query scope.", path=path))


readSchema = Refined(_operation("read", window=ref.optional(), ref=ref.optional(), offset=number(integer=True, min=0).optional(),
    menuPath=array(Trimmed(min=1, max=256), min=1, max=8).optional(), image=boolean().optional()), _readChoice)
commandSchema = Discriminated({MISSING: obj(dict(actions=array(actionSchema, min=1, max=12), image=boolean().optional(), window=ref.optional(), op=Undefined().optional())),
    "read": readSchema, "done": _operation("done", summary=Trimmed(min=1)), "handoff": _operation("handoff", reason=Trimmed(min=1))})

# These checked-in schemas are generated from the pre-migration contract. They
# are plain data; Python production never starts Node or imports its packages.
_model = jsonLoads(Path(__file__).with_name("model-schemas.json").read_text(encoding="utf-8"))
modelCommandParameters = _model["commands"]
modelDelegationParameters = _model["delegation"]
