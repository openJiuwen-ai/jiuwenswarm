from .._identity import FrozenRecord, WeakIdentityMap
from ..observation.text_source import text_source_label

_actions = WeakIdentityMap()
_TEXT_HANDLER = None


def action_result(receipt):
    result = FrozenRecord(dict(status=receipt["status"], detail=receipt["detail"]))
    _actions.set(result, receipt)
    return result


def result_receipt(value):
    return _actions.get(value)


def set_text_page_handler(handler):
    global _TEXT_HANDLER
    _TEXT_HANDLER = handler


def captured_text(page, source):
    result = FrozenRecord(page, display=text_source_label(source) + "\n" + repr(dict(page)))
    if _TEXT_HANDLER:
        _TEXT_HANDLER(source, result)
    return result
