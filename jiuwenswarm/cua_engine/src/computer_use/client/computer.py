"""Computer → App → Element: public intent and validation, independent of wire format."""
import weakref
from collections.abc import Mapping
from .._identity import FrozenRecord
from ..protocol.primitives import finite, safeInteger, nonempty, trim, utf16Length
from ..protocol.validation import validateText, validatePasteText, validateKey
from ..protocol.element_path import elementPath
from .diagnostics import localFailure
from .execution import operation
from .handles import handleCreated

_UNSET = object()


def _options(value, allowed): return isinstance(value, Mapping) and not set(value).difference(allowed)


def validateClick(options):
    if not _options(options, {"button", "count"}):
        raise localFailure("click options must contain only button and count", "use button left/right/middle and count 1/2")
    button, count = options.get("button", "left"), options.get("count", 1)
    if button not in ("left", "right", "middle") or type(count) not in (int, float) or count not in (1, 2):
        raise localFailure("click expects button left/right/middle and count 1/2", "correct the click options")
    return dict(button=button, count=count)


def validateScroll(direction, pages):
    if direction not in ("up", "down", "left", "right") or not finite(pages) or pages <= 0:
        raise localFailure("scroll expects direction up/down/left/right and finite positive pages", "use a direction and a positive page amount, for example down, 0.5")


def validateDrag(options):
    if not isinstance(options, Mapping) or set(options) != {"fromX", "fromY", "toX", "toY"} or any(not finite(v) or v < 0 for v in options.values()):
        raise localFailure("drag expects exactly four finite, nonnegative coordinates: fromX, fromY, toX, toY", "use start and end points from the same observed PNG")
    if options["fromX"] == options["toX"] and options["fromY"] == options["toY"]:
        raise localFailure("drag start and end must differ", "use click for a stationary press and release")


def observationState(state): return FrozenRecord(state, display=state["text"])


class Computer:
    """Current desktop. The native driver starts on the first system call."""
    def __init__(self):
        # Import lazily: the native controller uses these same public handles.
        # A desktop session has one v5 pipeline; failed actions never switch
        # back to the legacy high-level driver methods.
        from .native import NativeController
        self._controller, self._apps = NativeController(), weakref.WeakValueDictionary()
    def app(self, selector):
        try: nonempty(selector, "selector")
        except Exception as error: raise localFailure(f"app: {error}", "use an app name, identifier or path; listApps() can discover available apps") from error
        app = self._apps.get(selector)
        if app is not None and self._controller.scope(app)["selector"] != selector: app = None
        if app is None: app = App(self._controller, dict(selector=selector), _internal=True); self._apps[selector] = app
        return app
    def listApps(self): return self._controller.call("listApps", {})
    async def read(self, options=_UNSET):
        options = {} if options is _UNSET else options
        if not _options(options, {"full"}) or "full" in options and type(options["full"]) is not bool:
            raise localFailure("Computer.read expects options with an optional bool full", 'omit options or use {"full": True}')
        return await self._controller.readDesktop(options.get("full") is True)
    async def click(self, x, y, options=_UNSET):
        click = validateClick({} if options is _UNSET else options)
        self._controller.desktop.checkCoordinates(x, y)
        return await self._controller.act("click", dict(desktopRef=self._controller.desktop.target(True), x=x, y=y, **click))
    async def scroll(self, x, y, direction, pages=1):
        validateScroll(direction, pages); self._controller.desktop.checkCoordinates(x, y)
        return await self._controller.act("scroll", dict(desktopRef=self._controller.desktop.target(True), x=x, y=y, direction=direction, pages=pages))
    async def drag(self, options):
        validateDrag(options)
        self._controller.desktop.checkCoordinates(options["fromX"], options["fromY"])
        self._controller.desktop.checkCoordinates(options["toX"], options["toY"])
        return await self._controller.act("drag", dict(desktopRef=self._controller.desktop.target(True), **options))
    async def typeText(self, text):
        try: validateText(text)
        except Exception as error: raise localFailure(str(error), "use single-line Unicode; send control keys with pressKey") from error
        return await self._controller.act("typeText", dict(desktopRef=self._controller.desktop.target(), text=text))
    async def pressKey(self, key):
        try: validateKey(key)
        except Exception as error: raise localFailure(f"pressKey: {error}", "use the documented key names and modifiers") from error
        return await self._controller.act("pressKey", dict(desktopRef=self._controller.desktop.target(), key=key))


class App:
    def __init__(self, controller, scope, *, _internal=False):
        if not _internal: raise TypeError("Use Computer.app() to obtain an App")
        self._controller = controller
        controller.register(self, scope)
        handleCreated(self, dict(kind="App", controller=controller, selector=scope["selector"]))
    def window(self, ref): return self._controller.window(self, ref)
    async def read(self, options=_UNSET):
        options = {} if options is _UNSET else options
        if (not _options(options, {"full", "menuPath"}) or "full" in options and type(options["full"]) is not bool
            or "menuPath" in options and (not isinstance(options["menuPath"], list) or not 1 <= len(options["menuPath"]) <= 8
                or any(not isinstance(name, str) or not trim(name) or utf16Length(name) > 256 for name in options["menuPath"]))):
            raise localFailure("read expects { full?: boolean, menuPath?: string[] }", "select windows with app.window(ref); use observed menu titles for menuPath")
        return await self._controller.read(self, options.get("full") is True or "menuPath" in options, options["menuPath"][:] if "menuPath" in options else None)
    def element(self, ref): return Element(self._controller, self, ref, _internal=True)
    def find(self, path, options=_UNSET):
        options = {} if options is _UNSET else options
        try:
            normalized = elementPath(path)
            if not _options(options, {"timeoutMs"}) or "timeoutMs" in options and (not safeInteger(options["timeoutMs"]) or not 0 <= options["timeoutMs"] <= 10000):
                raise ValueError("find options must be { timeoutMs?: integer 0–10000 }")
            return Element(self._controller, self, dict(path=normalized, timeoutMs=options.get("timeoutMs", 3000)), _internal=True, _query=True)
        except Exception as error:
            raise localFailure(str(error), "use an observed semantic path; read and select a working window before operating it") from error
    async def click(self, x, y, options=_UNSET):
        if not finite(x) or not finite(y) or x < 0 or y < 0:
            raise localFailure("click coordinates must be finite, nonnegative numbers", "use coordinates from the latest App.read window PNG")
        click = validateClick({} if options is _UNSET else options)
        self._controller.checkCoordinates(self, x, y)
        return await self._controller.act("click", dict(appRef=self._controller.appRef(self, True), x=x, y=y, **click), self)
    async def typeText(self, text):
        try: validateText(text)
        except Exception as error: raise localFailure(str(error), "use a single line of Unicode text; send control keys separately with pressKey") from error
        return await self._controller.act("typeText", dict(**self._controller.keyboardTarget(self), text=text), self)
    async def paste(self, *args):
        if len(args) != 1: raise localFailure("App.paste expects only one text argument", "pass plain text; paste inserts at the caret/selection without replacing all or submitting")
        text = args[0]
        try: validatePasteText(text)
        except Exception as error: raise localFailure(str(error), "use valid Unicode text with optional tabs and line breaks") from error
        return await self._controller.act("paste", dict(**self._controller.keyboardTarget(self), text=text), self)
    async def scroll(self, x, y, direction, pages=1):
        validateScroll(direction, pages)
        if not finite(x) or not finite(y) or x < 0 or y < 0:
            raise localFailure("scroll coordinates must be finite, nonnegative numbers", "use coordinates from the latest App.read window PNG")
        self._controller.checkCoordinates(self, x, y, "scroll")
        return await self._controller.act("scroll", dict(appRef=self._controller.appRef(self, True), x=x, y=y, direction=direction, pages=pages), self)
    async def drag(self, options):
        validateDrag(options)
        self._controller.checkCoordinates(self, options["fromX"], options["fromY"], "drag")
        self._controller.checkCoordinates(self, options["toX"], options["toY"], "drag")
        return await self._controller.act("drag", dict(appRef=self._controller.appRef(self, True), **options), self)
    async def pressKey(self, key):
        try: validateKey(key)
        except Exception as error: raise localFailure(f"pressKey: {error}", "use the documented exact key names; correct the syntax instead of substituting a different key") from error
        return await self._controller.act("pressKey", dict(**self._controller.keyboardTarget(self), key=key), self)


class Element:
    def __init__(self, controller, owner, ref, *, _internal=False, _query=False):
        if not _internal: raise TypeError("Use App.element() or App.find() to obtain an Element")
        self._controller, self._owner = controller, owner
        self._query, self._snapshot = None, None
        self._readSnapshot = None
        if _query or not isinstance(ref, str): self._query = ref
        else:
            self._snapshot = controller.elementSnapshot(owner, ref)
        handleCreated(self, dict(kind="Element", controller=controller, owner=owner,
                                 snapshot=self._snapshot, query=self._query))
    async def _resolve(self, enabled):
        if self._query is not None: return await self._controller.find(self._owner, self._query["path"], self._query["timeoutMs"], enabled)
        return self._snapshot
    async def _act(self, intent, parameters=None):
        return await self._controller.actElement(self._owner, self._snapshot, self._query, intent, parameters or {})
    async def read(self, options=_UNSET):
        options = {} if options is _UNSET else options
        if (not _options(options, {"offset", "cursor"})
            or "offset" in options and (not safeInteger(options["offset"]) or options["offset"] < 0)
            or "cursor" in options and (not isinstance(options["cursor"], str) or not options["cursor"].strip() or len(options["cursor"]) > 256)
            or "offset" in options and "cursor" in options):
            raise localFailure("Element.read expects an optional offset or cursor, not both", "omit options to inspect this element; copy the continuation printed in its result")
        async def read():
            result, source = await self._controller.readElement(self._owner, self._snapshot, self._query,
                options.get('offset', 0), self._readSnapshot,
                **(dict(cursor=options['cursor']) if 'cursor' in options else {}))
            self._readSnapshot = source
            return result
        return await operation(read)
    async def readText(self, options=_UNSET):
        options = {} if options is _UNSET else options
        if (not _options(options, {"offset", "length"}) or "offset" in options and (not safeInteger(options["offset"]) or options["offset"] < 0)
            or "length" in options and (not safeInteger(options["length"]) or not 2 <= options["length"] <= 16384)):
            raise localFailure("Element.readText expects { offset?: nonnegative integer, length?: integer 2–16384 }", "omit options for the first page; pass the returned next offset to continue")
        async def read():
            if self._query is not None and options.get("offset", 0) == 0: self._snapshot = await self._resolve(False)
            if self._snapshot is None:
                raise localFailure("readText continuation needs a first page", "call readText() before using its returned next offset")
            return await self._controller.readText(self._owner, self._snapshot, options)
        return await operation(read)
    async def click(self, options=_UNSET):
        click = validateClick({} if options is _UNSET else options)
        return await self._act("click", click)
    async def openMenu(self, *args):
        if args: raise localFailure("Element.openMenu() accepts no arguments", "call element.openMenu() for a control that exposes a menu")
        return await self._act("openMenu")
    async def performAction(self, *args):
        if len(args) != 1: raise localFailure("Element.performAction expects one action name", "copy an action shown on this element under Secondary Actions")
        action = args[0]
        try: nonempty(action, "action")
        except Exception as error: raise localFailure(str(error), "copy an exact action shown on this element under Secondary Actions") from error
        return await self._act("action", dict(action=action))
    async def pressKey(self, key):
        try: validateKey(key)
        except Exception as error: raise localFailure(f"pressKey: {error}", "use the documented key names and modifiers") from error
        return await self._act("key", dict(key=key))
    async def focus(self, *args):
        if args: raise localFailure("Element.focus() accepts no arguments", "call element.focus() for the observed control")
        return await self._act("focus")
    async def scroll(self, direction, pages=1):
        validateScroll(direction, pages)
        return await self._act("scroll", dict(direction=direction, pages=pages))
    async def typeText(self, text, options=_UNSET):
        try: validateText(text)
        except Exception as error: raise localFailure(str(error), "use single-line text; submit separately with App.pressKey") from error
        options = {} if options is _UNSET else options
        if not _options(options, {"replace"}) or "replace" in options and type(options["replace"]) is not bool:
            raise localFailure("Element.typeText expects options with an optional bool replace", 'use {"replace": True} to replace the input contents, or omit it to insert')
        return await self._act("replace" if options.get("replace", False) else "insert", dict(text=text))
    async def paste(self, *args):
        if len(args) != 1: raise localFailure("Element.paste expects only one text argument", "pass plain text; paste inserts at the caret/selection without replacing all or submitting")
        text = args[0]
        try: validatePasteText(text)
        except Exception as error: raise localFailure(str(error), "use valid Unicode text with optional tabs and line breaks") from error
        return await self._act("paste", dict(text=text))


def computerForController(controller):
    computer = object.__new__(Computer)
    computer._controller, computer._apps = controller, weakref.WeakValueDictionary()
    return computer
def selectWorkingWindow(app, ref): return app._controller.workingWindow(app, ref)
