"""Tests for FeishuChannel import fallback when the lark_oapi SDK is absent.

``feishu_connect`` guards its ``lark_oapi`` imports with ``try/except
ImportError`` and exposes ``FEISHU_AVAILABLE`` so the rest of the gateway can
run without the Feishu SDK -- the same pattern used by the Telegram, Discord
and DingTalk channels. These tests pin that contract down.
"""

import importlib
import sys

import pytest

MODULE = "jiuwenswarm.gateway.channel_manager.im_platforms.feishu.feishu_connect"


@pytest.fixture
def without_lark_sdk():
    """Reload the module as if lark_oapi were not installed."""
    saved = {
        name: mod
        for name, mod in sys.modules.items()
        if name == "lark_oapi" or name.startswith("lark_oapi.")
    }
    saved[MODULE] = sys.modules.get(MODULE)
    for name in saved:
        sys.modules.pop(name, None)
    # A ``None`` entry makes ``import lark_oapi`` raise ImportError.
    sys.modules["lark_oapi"] = None
    try:
        yield
    finally:
        sys.modules.pop("lark_oapi", None)
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod
        importlib.import_module(MODULE)


def test_import_without_lark_sdk_degrades(without_lark_sdk):
    """Import must degrade gracefully instead of raising NameError.

    ``P2CardActionTrigger`` / ``P2CardActionTriggerResponse`` are referenced in
    ``_on_card_action_trigger_sync``'s signature. Annotations are evaluated when
    the class body runs, so leaving them unbound in the ``except ImportError``
    branch turns a missing optional SDK into a module-level NameError.
    """
    module = importlib.import_module(MODULE)

    assert module.FEISHU_AVAILABLE is False
    assert module.P2CardActionTrigger is None
    assert module.P2CardActionTriggerResponse is None


def test_channel_classes_usable_without_lark_sdk(without_lark_sdk):
    """The channel classes stay importable and constructible after the fallback."""
    module = importlib.import_module(MODULE)

    config = module.FeishuConfig(enabled=True, app_id="app", app_secret="secret")
    assert config.channel_id == "feishu"
    assert hasattr(module.FeishuChannel, "_on_card_action_trigger_sync")
