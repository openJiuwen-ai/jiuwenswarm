_handler = None


def set_action_handler(handler):
    global _handler
    _handler = handler


def action_completed(receipt):
    try:
        if _handler:
            _handler(receipt)
    except Exception as error:
        raise RuntimeError((f"Action completed, but its receipt could not be displayed: {error}\nEffect: "
                            f"{receipt['target']}: {receipt['detail']}\nNext: read the app before "
                            f"continuing; do not replay the action")) from error
