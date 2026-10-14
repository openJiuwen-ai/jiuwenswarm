_handler = None


def setActionHandler(handler):
    global _handler
    _handler = handler


def actionCompleted(receipt):
    try:
        if _handler: _handler(receipt)
    except Exception as error:
        raise RuntimeError(f"Action completed, but its receipt could not be displayed: {error}\nEffect: {receipt['target']}: {receipt['detail']}\nNext: read the app before continuing; do not replay the action") from error
