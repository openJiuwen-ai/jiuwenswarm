from .._identity import FrozenRecord, WeakIdentityMap
from ..observation.text_source import textSourceLabel

_actions = WeakIdentityMap()
_textHandler = None


def actionResult(receipt):
    result = FrozenRecord(dict(status=receipt["status"], detail=receipt["detail"]))
    _actions.set(result, receipt)
    return result


def resultReceipt(value): return _actions.get(value)


def setTextPageHandler(handler):
    global _textHandler
    _textHandler = handler


def capturedText(page, source):
    result = FrozenRecord(page, display=textSourceLabel(source) + "\n" + repr(dict(page)))
    if _textHandler: _textHandler(source, result)
    return result
