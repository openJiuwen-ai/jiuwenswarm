"""The library boundary, pinned (§25.5, B0's acceptance criterion).

The clouddoc package is on its way to the standalone mandate-mcp repository, and
the extraction is a move rather than a rewrite exactly as long as these hold. A
new import that breaks one of them is not a style problem -- it is a weld to the
host that someone will have to cut again later, under worse conditions.
"""

from __future__ import annotations

import re
from pathlib import Path

PKG = Path("jiuwenswarm/extensions/co_scribe/backend/toolkit")

# The two documented exceptions, each a *lazy default* a constructor can override:
# the IC-3 grant checker (cut ③) and the deployment seam (cut ④). Everything else
# in the package must not know the host exists.
ALLOWED = {
    ("clouddoc_tools.py", "jiuwenswarm.extensions.co_scribe.backend.host.authority.watch_registry"),
    ("deployment.py", "jiuwenswarm.common.config"),
    ("deployment.py", "jiuwenswarm.common.utils"),
}


# The host half of co-scribe moved into the plugin with the rest of it and is
# named for what it is now, so a reverse import reads ``...co_scribe.backend.host``
# -- but the real gateway package is still a weld if anything in here reaches for
# it, so both prefixes stay under watch.
GATEWAY_HALF = "jiuwenswarm.extensions.co_scribe.backend.host"


def _imports(path: Path) -> set[str]:
    src = path.read_text(encoding="utf-8")
    return set(re.findall(
        r"(?:from|import)\s+("
        r"jiuwenswarm\.extensions\.co_scribe\.backend\.host[\w.]*"
        r"|jiuwenswarm\.gateway[\w.]*"
        r"|jiuwenswarm\.common[\w.]*"
        r"|openjiuwen[\w.]*)",
        src,
    ))


def test_the_library_layer_stays_host_free():
    offences = []
    for f in sorted(PKG.rglob("*.py")):
        for mod in _imports(f):
            if mod.startswith("openjiuwen"):
                offences.append(f"{f.name}: {mod} (host framework)")
                continue
            if any(f.name == fn and mod.startswith(allowed) for fn, allowed in ALLOWED):
                continue
            reverse = mod.startswith((GATEWAY_HALF, "jiuwenswarm.gateway"))
            kind = "reverse import" if reverse else "deployment leak"
            offences.append(f"{f.name}: {mod} ({kind})")
    assert not offences, "库层出现宿主焊点：\n" + "\n".join(offences)


def test_the_bridge_lives_outside_the_package():
    """openjiuwen translation is jiuwenswarm's own adapter and stays behind at
    extraction; inside the package it would defeat the point of cut ①."""
    bridge = Path("jiuwenswarm/extensions/co_scribe/backend/clouddoc_bridge.py")
    assert bridge.exists()
    assert "openjiuwen" in bridge.read_text(encoding="utf-8")
    assert not (PKG / "clouddoc_bridge.py").exists()


def test_every_translation_key_the_ui_asks_for_exists():
    """A key the code calls by name must be in the locale file.

    The parity check beside this one compares the two languages against each other,
    and there is a failure it structurally cannot see: a key deleted from **both**
    sides stays in parity and disappears from the product. That happened -- a regex
    written to remove ``docs.kind.unsupported`` also matched ``docs.unsupported``,
    the line the table foot shows when a shared file is in a format co-scribe does
    not serve, and nothing noticed: the two languages still agreed, no test covered
    the surface, and it only renders when such a file is actually shared.

    So this reads the other direction -- from the call sites to the file. Literal
    ``t('...')`` calls only; keys built from a template are checked by the tests that
    exercise those paths, and guessing at their expansions here would trade a real
    check for a brittle one.
    """
    import json

    root = Path(__file__).resolve().parents[2] / "frontend"
    # parents[4] is the ``jiuwenswarm`` package; the locales sit under its web channel.
    locale = (
        Path(__file__).resolve().parents[4]
        / "channels/web/frontend/src/i18n/locales/zh.json"
    )
    known: set[str] = set()

    def walk(node, prefix=""):
        if isinstance(node, dict):
            for key, value in node.items():
                walk(value, f"{prefix}{key}.")
        else:
            known.add(prefix[:-1])

    walk(json.loads(locale.read_text(encoding="utf-8")))

    asked: dict[str, str] = {}
    for path in list(root.rglob("*.tsx")) + list(root.rglob("*.ts")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        # The lookbehind matters: ``webRequest('clouddoc.watch_set')`` and
        # ``.split('.')`` both end in ``t(`` and are not translations.
        for match in re.finditer(r"(?<![\w.])t\(\s*'([^']+)'", text):
            asked.setdefault(match.group(1), path.name)

    missing = sorted(k for k in asked if k not in known)
    assert not missing, "界面用到但语言包里没有的 key：" + ", ".join(
        f"{k} ({asked[k]})" for k in missing
    )


def test_the_workbench_can_answer_an_authorization_prompt():
    """Where a write is started is where it has to be approvable.

    The workbench docks its own composer under the document, so a person working there
    starts writes there -- and the authorization prompt lived in exactly one place, the
    main chat's slot. From the workbench the input greyed itself out and said to deal
    with the item above; there was no item above. The write could not be approved, could
    not be refused, and nothing on screen explained why (measured on a live turn,
    2026-09-10).

    Checked at the source rather than through a render: what went wrong was a component
    that was never mounted, which a unit test of the component itself cannot see.
    """
    import pathlib

    root = pathlib.Path("jiuwenswarm/extensions/co_scribe/frontend/DocWorkbench")
    strip = (root / "ChatStrip.tsx").read_text(encoding="utf-8")
    assert "InteractionSlot" in strip, "工作台的聊天条必须自带审批位"
    assert "<InteractionSlot" in strip, "导入还不够——它必须真的被渲染"
    assert strip.index("<InteractionSlot") < strip.index("<InputArea"), (
        "审批位要在输入框上方：输入框停用时提示的是「上方待确认项」"
    )
    board = (root / "index.tsx").read_text(encoding="utf-8")
    assert "onUserAnswer" in board, "工作台必须把作答回调透传给聊天条"
