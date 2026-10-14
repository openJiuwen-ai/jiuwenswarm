"""Command results and input validation, equivalent to protocol/validation.ts."""
from __future__ import annotations

import re

from .action_context import decodeActionContext
from .captures import captureMetadata, decodeCapture, decodeStamp
from .desktop import decodeDesktopObservation
from .observation import decodeObservation
from .png import pngSize
from .primitives import frozen, nonempty, record, reference, safeInteger
from .text import decodeTextPage


def _unicode(value):
    # A pair of explicit surrogate code points is also valid JS Unicode input.
    try:
        value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "strict")
        return True
    except UnicodeError:
        return False


def validateText(value):
    if not isinstance(value, str) or re.search(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", value):
        raise ValueError("typeText expects one line of text; use pressKey for control keys")
    if not _unicode(value):
        raise ValueError("typeText expects valid Unicode")


def validatePasteText(value):
    if not isinstance(value, str) or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", value):
        raise ValueError("paste expects plain text; only tabs and line breaks are allowed control characters")
    if not _unicode(value):
        raise ValueError("paste expects valid Unicode")


_ORDINARY = ["Return", "Tab", "Escape", "BackSpace", "Delete", "space", "Left", "Right", "Up", "Down", "Home", "End", "Page_Up", "Page_Down", *[f"F{i}" for i in range(1, 13)]]


def validateKey(value):
    nonempty(value, "key")
    *parts, key = value.split("+")
    if (not (key in _ORDINARY or re.fullmatch(r"[a-z0-9]", key)) or len(set(parts)) != len(parts)
            or any(p not in ("super", "ctrl", "alt", "shift") for p in parts)):
        raise ValueError(f"unsupported key or modifiers: {value}. Use a–z, 0–9, {', '.join(_ORDINARY)}; modifiers: super, ctrl, alt, shift (joined with +)")


def decodeResult(method, value):
    if method == "findElement":
        result = record(value, ["appRef", "scope", "ref", "token", "observed"])
        for key in ("appRef", "scope", "token"):
            nonempty(result[key], "protocol " + key)
        if not reference(result["ref"]):
            raise ValueError("protocol: invalid findElement reference")
        return {**result, "observed": decodeStamp(result["observed"])}
    if method == "readElementText":
        return decodeTextPage(value)
    if method == "readDesktop":
        return decodeDesktopObservation(value)
    if method == "listApps":
        if not isinstance(value, list):
            raise ValueError("protocol: invalid app list")
        ids, apps = set(), []
        for item in value:
            app = record(item, ["id", "displayName", "isRunning"])
            nonempty(app["id"], "protocol app id")
            nonempty(app["displayName"], "protocol app displayName")
            if type(app["isRunning"]) is not bool or app["id"] in ids:
                raise ValueError("protocol: invalid or duplicate app entry")
            ids.add(app["id"])
            apps.append(app)
        return apps
    if method == "readApp":
        hasOverview = isinstance(value, dict) and "overview" in value
        hasCatalogScope = isinstance(value, dict) and "catalogScope" in value
        result = record(value, ["instance", "appRef", "observed", "scope", "windows", "window", "observation", "screenshot", "refs",
                                *(["overview"] if hasOverview else []), *(["catalogScope"] if hasCatalogScope else [])])
        nonempty(result["instance"], "protocol application instance")
        if hasCatalogScope:
            nonempty(result["catalogScope"], "protocol window catalogue scope")
        nonempty(result["appRef"], "protocol appRef")
        nonempty(result["scope"], "protocol window scope")
        observed = decodeStamp(result["observed"])
        if not isinstance(result["windows"], dict):
            raise ValueError("protocol: invalid window catalog")
        windows, scopes = result["windows"], set()
        for ref, scope in windows.items():
            if not reference(ref):
                raise ValueError("protocol: invalid window reference")
            nonempty(scope, "protocol window scope")
            if scope in scopes:
                raise ValueError("protocol: duplicate window scope")
            scopes.add(scope)
        window = result["window"]
        if window is not None and (not isinstance(window, str) or window not in windows or windows[window] != result["scope"]):
            raise ValueError("protocol: selected window does not match its scope")
        if not isinstance(result["refs"], dict):
            raise ValueError("protocol: invalid readApp result")
        tokens = set()
        for name, token in result["refs"].items():
            if not reference(name):
                raise ValueError("protocol: invalid numeric element reference")
            nonempty(token, "protocol ref")
            if token in tokens:
                raise ValueError("protocol: duplicate element token")
            tokens.add(token)
        observation = decodeObservation(result["observation"])
        screenshot = None if result["screenshot"] is None else decodeCapture(result["screenshot"], "window", result["scope"], observed)
        overview = None if not hasOverview or result["overview"] is None else decodeCapture(result["overview"], "overview", observed=observed)
        if ((screenshot is not None) != ("screenshot" in observation)
                or (screenshot is not None and pngSize(screenshot) != observation["screenshot"])):
            raise ValueError("protocol: observation screenshot dimensions do not match its capture")
        ids = [observed["id"], *[captureMetadata(png)["id"] for png in (screenshot, overview) if png is not None]]
        if len(set(ids)) != len(ids):
            raise ValueError("protocol: duplicate observation or image identity")
        if observation["current"] != window or sorted(w["ref"] for w in observation["windows"]) != sorted(windows):
            raise ValueError("protocol: inconsistent observation window catalog")
        return dict(instance=result["instance"], **({"catalogScope": result["catalogScope"]} if hasCatalogScope else {}),
                    appRef=result["appRef"], observed=observed, scope=result["scope"], windows=windows, window=window,
                    observation=observation, screenshot=screenshot, overview=overview, refs=result["refs"])
    receipt = record(value, ["target", "status", "detail", "context"])
    nonempty(receipt["target"], "protocol action target")
    nonempty(receipt["detail"], "protocol action detail")
    if receipt["status"] not in ("dispatched", "verified"):
        raise ValueError("protocol: invalid action status")
    return frozen({**receipt, "context": decodeActionContext(receipt["context"])})
