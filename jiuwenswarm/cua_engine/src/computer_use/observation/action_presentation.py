from ..protocol.primitives import trim_end


class ActionPresentation:
    """One tool result's bounded receipt registry; does not survive that result."""

    def __init__(self):
        self._details, self._receipts, self._desktop, self._next = {}, {}, {}, 1

    def has(self, receipt):
        return self._receipts.get(id(receipt)) is receipt

    def render(self, receipt):
        number, previous = self._next, self._details.get(receipt["detail"])
        self._next += 1
        detail = receipt["detail"] if previous is None else f"Result detail: same as action #{previous}."
        desktop_line = ""
        if previous is None:
            suffix = []
            for statement in ("Original pointer restored.", "Foreground input used."):
                if detail.endswith(" " + statement):
                    suffix.insert(0, statement)
                    detail = trim_end(detail[:-len(statement)])
            if suffix:
                facts = " ".join(suffix)
                existing = self._desktop.get(facts)
                identifier = existing if existing is not None else len(self._desktop) + 1
                self._desktop[facts] = identifier
                detail += f" Desktop: #{identifier}."
                if existing is None:
                    desktop_line = f"\nDesktop #{identifier}: {facts}"
        self._details[receipt["detail"]] = previous if previous is not None else number
        self._receipts[id(receipt)] = receipt
        return f"Action [{receipt['status']}]: #{number} {receipt['target']}. {detail}{desktop_line}"
