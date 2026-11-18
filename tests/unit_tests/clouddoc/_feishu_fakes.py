"""Stand-ins shared by the Feishu reader and provider tests.

A CLI that answers from a script and records what it was asked, a CLI that refuses
one verb, and a factory that wires either provider class to one of them.
"""

from __future__ import annotations

import json

from jiuwenswarm.clouddoc.providers.base import ProviderError
from jiuwenswarm.clouddoc.providers.feishu.cli import LarkCli, LarkResult

DOC = "DocToken123456"


class _StubCli(LarkCli):
    """A CLI that answers from a script and records what it was asked."""

    def __init__(self, replies: dict | None = None) -> None:
        super().__init__(binary="lark-cli", profile="p")
        self.calls: list[list[str]] = []
        self._replies = replies or {}
        self.fail_with: ProviderError | None = None

    @property
    def available(self) -> bool:
        return True

    async def run(self, args, *, timeout=None) -> LarkResult:
        self.calls.append(list(args))
        if self.fail_with is not None:
            return LarkResult(stdout="", stderr=str(self.fail_with), code=1)
        key = " ".join(args[:2])
        payload = self._replies.get(key)
        # The real CLI answers in an envelope; the stub does too, or the code under
        # test would be exercised against a shape it never sees.
        return LarkResult(
            stdout=json.dumps({"ok": True, "identity": "bot", "data": payload or {}}),
            stderr="", code=0,
        )

    async def json(self, args, *, timeout=None):
        res = await self.run(args, timeout=timeout)
        if not res.ok:
            raise self.fail_with or ProviderError("unknown", res.stderr)
        return json.loads(res.stdout).get("data")


# ------------------------------------------- capabilities the tenant decides, at runtime


class _RefusingCli(_StubCli):
    """A CLI that refuses one command and answers the rest, as a tenant policy would."""

    def __init__(self, refuse_prefix: str, kind: str = "forbidden", replies=None) -> None:
        super().__init__(replies)
        self._refuse = refuse_prefix
        self._kind = kind

    async def json(self, args, *, timeout=None):
        self.calls.append(list(args))
        if " ".join(args[:2]) == self._refuse:
            raise ProviderError(self._kind, "policy refuses this")
        payload = self._replies.get(" ".join(args[:2])) or {}
        return payload


# ------------------------------- the plumbing the rest of the machinery expects


# ------------------------------------------------ review v1 of the provider (#11725)


def provider_with(cls, replies: dict | None = None):
    """An instance of ``cls`` whose CLI is a scripted stand-in, and that stand-in."""
    p = cls(profile="p", self_open_id="ou_bot")
    stub = _StubCli(replies)
    p.cli = stub
    return p, stub
