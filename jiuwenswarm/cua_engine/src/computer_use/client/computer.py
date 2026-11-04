"""Computer → App → Element: public intent and validation, independent of wire format."""
import weakref
from collections.abc import Mapping

from .._identity import FrozenRecord
from ..protocol.element_path import element_path
from ..protocol.primitives import finite, nonempty, safe_integer, trim, utf16_length
from ..protocol.validation import validate_key, validate_paste_text, validate_text
from .diagnostics import local_failure
from .execution import operation
from .handles import handle_created

_UNSET = object()


def _options(value, allowed):
    return isinstance(value, Mapping) and not set(value).difference(allowed)


def validate_click(options):
    if not _options(options, {"button", "count"}):
        raise local_failure("click options must contain only button and count",
                            "use button left/right/middle and count 1/2")
    button, count = options.get("button", "left"), options.get("count", 1)
    if button not in ("left", "right", "middle") or type(count) not in (int, float) or count not in (1, 2):
        raise local_failure("click expects button left/right/middle and count 1/2", "correct the click options")
    return dict(button=button, count=count)


def validate_scroll(direction, pages):
    if direction not in ("up", "down", "left", "right") or not finite(pages) or pages <= 0:
        raise local_failure("scroll expects direction up/down/left/right and finite positive pages",
                            "use a direction and a positive page amount, for example down, 0.5")


def validate_drag(options):
    if not isinstance(options, Mapping) or set(options) != {"fromX", "fromY", "toX", "toY"} or any(
            not finite(v) or v < 0 for v in options.values()):
        raise local_failure("drag expects exactly four finite, nonnegative coordinates: fromX, fromY, toX, toY",
                            "use start and end points from the same observed PNG")
    if options["fromX"] == options["toX"] and options["fromY"] == options["toY"]:
        raise local_failure("drag start and end must differ", "use click for a stationary press and release")


def observation_state(state):
    return FrozenRecord(state, display=state["text"])


class Computer:
    """Current desktop. The native driver starts on the first system call."""

    def __init__(self):
        # Import lazily: the native controller uses these same public handles.
        # A desktop session has one v5 pipeline; failed actions never switch
        # back to the legacy high-level driver methods.
        from .native import NativeController
        self._controller, self._apps = NativeController(), weakref.WeakValueDictionary()

    def app(self, selector):
        try:
            nonempty(selector, "selector")
        except Exception as error:
            raise local_failure(
                f"app: {error}",
                "use an app name, identifier or path; listApps() can discover available apps") from error
        app = self._apps.get(selector)
        if app is not None and self._controller.scope(app)["selector"] != selector:
            app = None
        if app is None:
            app = App(self._controller, dict(selector=selector), _internal=True)
            self._apps[selector] = app
        return app

    # Retain the established Agent API spelling.
    # pylint: disable-next=huawei-invalid-name
    def listApps(self):
        return self._controller.call("listApps", {})

    async def read(self, options=_UNSET):
        options = {} if options is _UNSET else options
        if not _options(options, {"full"}) or "full" in options and not isinstance(options["full"], bool):
            raise local_failure("Computer.read expects options with an optional bool full",
                                'omit options or use {"full": True}')
        return await self._controller.read_desktop(options.get("full") is True)

    async def click(self, x, y, options=_UNSET):
        click = validate_click({} if options is _UNSET else options)
        self._controller.desktop.check_coordinates(x, y)
        return await self._controller.act("click",
                                          dict(desktopRef=self._controller.desktop.target(True), x=x, y=y, **click))

    async def scroll(self, x, y, direction, pages=1):
        validate_scroll(direction, pages)
        self._controller.desktop.check_coordinates(x, y)
        return await self._controller.act(
            "scroll", dict(desktopRef=self._controller.desktop.target(True), x=x, y=y, direction=direction,
                           pages=pages))

    async def drag(self, options):
        validate_drag(options)
        self._controller.desktop.check_coordinates(options["fromX"], options["fromY"])
        self._controller.desktop.check_coordinates(options["toX"], options["toY"])
        return await self._controller.act("drag", dict(desktopRef=self._controller.desktop.target(True), **options))

    # Retain the established Agent API spelling.
    # pylint: disable-next=huawei-invalid-name
    async def typeText(self, text):
        try:
            validate_text(text)
        except Exception as error:
            raise local_failure(str(error), "use single-line Unicode; send control keys with pressKey") from error
        return await self._controller.act("typeText", dict(desktopRef=self._controller.desktop.target(), text=text))

    # Retain the established Agent API spelling.
    # pylint: disable-next=huawei-invalid-name
    async def pressKey(self, key):
        try:
            validate_key(key)
        except Exception as error:
            raise local_failure(f"pressKey: {error}", "use the documented key names and modifiers") from error
        return await self._controller.act("pressKey", dict(desktopRef=self._controller.desktop.target(), key=key))


class App:

    def __init__(self, controller, scope, *, _internal=False):
        if not _internal:
            raise TypeError("Use Computer.app() to obtain an App")
        self._controller = controller
        controller.register(self, scope)
        handle_created(self, dict(kind="App", controller=controller, selector=scope["selector"]))

    def window(self, ref):
        return self._controller.window(self, ref)

    async def read(self, options=_UNSET):
        options = {} if options is _UNSET else options
        invalid_read_options = (not _options(options, {"full", "menuPath"})
                                or "full" in options and not isinstance(options["full"], bool)
                                or "menuPath" in options and
                                (not isinstance(options["menuPath"], list) or not 1 <= len(options["menuPath"]) <= 8
                                 or any(not isinstance(name, str) or not trim(name) or utf16_length(name) > 256
                                        for name in options["menuPath"])))
        if invalid_read_options:
            raise local_failure("read expects { full?: boolean, menuPath?: string[] }",
                                "select windows with app.window(ref); use observed menu titles for menuPath")
        return await self._controller.read(self,
                                           options.get("full") is True or "menuPath" in options,
                                           options["menuPath"][:] if "menuPath" in options else None)

    def element(self, ref):
        return Element(self._controller, self, ref, _internal=True)

    def find(self, path, options=_UNSET):
        options = {} if options is _UNSET else options
        try:
            normalized = element_path(path)
            invalid_find_options = not _options(options, {"timeoutMs"}) or "timeoutMs" in options and (
                not safe_integer(options["timeoutMs"]) or not 0 <= options["timeoutMs"] <= 10000)
            if invalid_find_options:
                raise ValueError("find options must be { timeoutMs?: integer 0–10000 }")
            return Element(self._controller, self, dict(path=normalized, timeoutMs=options.get("timeoutMs", 3000)),
                           _internal=True, _query=True)
        except Exception as error:
            raise local_failure(
                str(error),
                "use an observed semantic path; read and select a working window before operating it") from error

    async def click(self, x, y, options=_UNSET):
        invalid_coordinates = not finite(x) or not finite(y) or x < 0 or y < 0
        if invalid_coordinates:
            raise local_failure("click coordinates must be finite, nonnegative numbers",
                                "use coordinates from the latest App.read window PNG")
        click = validate_click({} if options is _UNSET else options)
        self._controller.check_coordinates(self, x, y)
        return await self._controller.act("click", dict(appRef=self._controller.app_ref(self, True), x=x, y=y, **click),
                                          self)

    # Retain the established Agent API spelling.
    # pylint: disable-next=huawei-invalid-name
    async def typeText(self, text):
        try:
            validate_text(text)
        except Exception as error:
            raise local_failure(
                str(error), "use a single line of Unicode text; send control keys separately with pressKey") from error
        return await self._controller.act("typeText", dict(**self._controller.keyboard_target(self), text=text), self)

    async def paste(self, *args):
        if len(args) != 1:
            raise local_failure(
                "App.paste expects only one text argument",
                "pass plain text; paste inserts at the caret/selection without replacing all or submitting")
        text = args[0]
        try:
            validate_paste_text(text)
        except Exception as error:
            raise local_failure(str(error), "use valid Unicode text with optional tabs and line breaks") from error
        return await self._controller.act("paste", dict(**self._controller.keyboard_target(self), text=text), self)

    async def scroll(self, x, y, direction, pages=1):
        validate_scroll(direction, pages)
        invalid_coordinates = not finite(x) or not finite(y) or x < 0 or y < 0
        if invalid_coordinates:
            raise local_failure("scroll coordinates must be finite, nonnegative numbers",
                                "use coordinates from the latest App.read window PNG")
        self._controller.check_coordinates(self, x, y, "scroll")
        return await self._controller.act(
            "scroll", dict(appRef=self._controller.app_ref(self, True), x=x, y=y, direction=direction, pages=pages),
            self)

    async def drag(self, options):
        validate_drag(options)
        self._controller.check_coordinates(self, options["fromX"], options["fromY"], "drag")
        self._controller.check_coordinates(self, options["toX"], options["toY"], "drag")
        return await self._controller.act("drag", dict(appRef=self._controller.app_ref(self, True), **options), self)

    # Retain the established Agent API spelling.
    # pylint: disable-next=huawei-invalid-name
    async def pressKey(self, key):
        try:
            validate_key(key)
        except Exception as error:
            raise local_failure(
                f"pressKey: {error}",
                "use the documented exact key names; correct the syntax instead of substituting a different key"
            ) from error
        return await self._controller.act("pressKey", dict(**self._controller.keyboard_target(self), key=key), self)


class Element:

    def __init__(self, controller, owner, ref, *, _internal=False, _query=False):
        if not _internal:
            raise TypeError("Use App.element() or App.find() to obtain an Element")
        self._controller, self._owner = controller, owner
        self._query, self._snapshot = None, None
        self._read_snapshot = None
        if _query or not isinstance(ref, str):
            self._query = ref
        else:
            self._snapshot = controller.element_snapshot(owner, ref)
        handle_created(
            self, dict(kind="Element", controller=controller, owner=owner, snapshot=self._snapshot, query=self._query))

    async def _resolve(self, enabled):
        if self._query is not None:
            return await self._controller.find(self._owner, self._query["path"], self._query["timeoutMs"], enabled)
        return self._snapshot

    async def _act(self, intent, parameters=None):
        return await self._controller.act_element(self._owner, self._snapshot, self._query, intent, parameters or {})

    async def read(self, options=_UNSET):
        options = {} if options is _UNSET else options
        invalid_element_options = (
            not _options(options, {"offset", "cursor"})
            or "offset" in options and (not safe_integer(options["offset"]) or options["offset"] < 0)
            or "cursor" in options and
            (not isinstance(options["cursor"], str) or not options["cursor"].strip() or len(options["cursor"]) > 256)
            or "offset" in options and "cursor" in options)
        if invalid_element_options:
            raise local_failure("Element.read expects an optional offset or cursor, not both",
                                "omit options to inspect this element; copy the continuation printed in its result")

        async def read():
            result, source = await self._controller.read_element(
                self._owner, self._snapshot, self._query, options.get('offset', 0), self._read_snapshot,
                **(dict(cursor=options['cursor']) if 'cursor' in options else {}))
            self._read_snapshot = source
            return result

        return await operation(read)

    # Retain the established Agent API spelling.
    # pylint: disable-next=huawei-invalid-name
    async def readText(self, options=_UNSET):
        options = {} if options is _UNSET else options
        invalid_text_options = (not _options(options, {"offset", "length"}) or "offset" in options and
                                (not safe_integer(options["offset"]) or options["offset"] < 0)
                                or "length" in options and
                                (not safe_integer(options["length"]) or not 2 <= options["length"] <= 16384))
        if invalid_text_options:
            raise local_failure("Element.readText expects { offset?: nonnegative integer, length?: integer 2–16384 }",
                                "omit options for the first page; pass the returned next offset to continue")

        async def read():
            if self._query is not None and options.get("offset", 0) == 0:
                self._snapshot = await self._resolve(False)
            if self._snapshot is None:
                raise local_failure("readText continuation needs a first page",
                                    "call readText() before using its returned next offset")
            return await self._controller.readText(self._owner, self._snapshot, options)

        return await operation(read)

    async def click(self, options=_UNSET):
        click = validate_click({} if options is _UNSET else options)
        return await self._act("click", click)

    # Retain the established Agent API spelling.
    # pylint: disable-next=huawei-invalid-name
    async def openMenu(self, *args):
        if args:
            raise local_failure("Element.openMenu() accepts no arguments",
                                "call element.openMenu() for a control that exposes a menu")
        return await self._act("openMenu")

    # Retain the established Agent API spelling.
    # pylint: disable-next=huawei-invalid-name
    async def performAction(self, *args):
        if len(args) != 1:
            raise local_failure("Element.performAction expects one action name",
                                "copy an action shown on this element under Secondary Actions")
        action = args[0]
        try:
            nonempty(action, "action")
        except Exception as error:
            raise local_failure(str(error),
                                "copy an exact action shown on this element under Secondary Actions") from error
        return await self._act("action", dict(action=action))

    # Retain the established Agent API spelling.
    # pylint: disable-next=huawei-invalid-name
    async def pressKey(self, key):
        try:
            validate_key(key)
        except Exception as error:
            raise local_failure(f"pressKey: {error}", "use the documented key names and modifiers") from error
        return await self._act("key", dict(key=key))

    async def focus(self, *args):
        if args:
            raise local_failure("Element.focus() accepts no arguments", "call element.focus() for the observed control")
        return await self._act("focus")

    async def scroll(self, direction, pages=1):
        validate_scroll(direction, pages)
        return await self._act("scroll", dict(direction=direction, pages=pages))

    # Retain the established Agent API spelling.
    # pylint: disable-next=huawei-invalid-name
    async def typeText(self, text, options=_UNSET):
        try:
            validate_text(text)
        except Exception as error:
            raise local_failure(str(error), "use single-line text; submit separately with App.pressKey") from error
        options = {} if options is _UNSET else options
        if not _options(options, {"replace"}) or "replace" in options and not isinstance(options["replace"], bool):
            raise local_failure("Element.typeText expects options with an optional bool replace",
                                'use {"replace": True} to replace the input contents, or omit it to insert')
        return await self._act("replace" if options.get("replace", False) else "insert", dict(text=text))

    async def paste(self, *args):
        if len(args) != 1:
            raise local_failure(
                "Element.paste expects only one text argument",
                "pass plain text; paste inserts at the caret/selection without replacing all or submitting")
        text = args[0]
        try:
            validate_paste_text(text)
        except Exception as error:
            raise local_failure(str(error), "use valid Unicode text with optional tabs and line breaks") from error
        return await self._act("paste", dict(text=text))


def computer_for_controller(controller):
    computer = object.__new__(Computer)
    # SDK host/registry code shares state owned by these exact handle objects.
    # pylint: disable-next=protected-access
    computer._controller, computer._apps = controller, weakref.WeakValueDictionary()
    return computer


def select_working_window(app, ref):
    # SDK host/registry code shares state owned by these exact handle objects.
    # pylint: disable-next=protected-access
    return app._controller.working_window(app, ref)
