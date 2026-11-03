"""Shared fixtures for the co-scribe unit tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _clouddoc_config_is_not_the_developers(monkeypatch):
    """No test result may depend on the machine's own ``config.yaml``.

    Several modules read ``clouddoc.enabled`` and ``clouddoc.mode`` live, so a test
    that does not patch the configuration itself would inherit whatever the developer
    has configured and answer differently on two machines. A deployment with the
    feature on, in the only mode that has an unattended path; a test that needs
    another mode patches ``get_config`` itself, and that patch is applied after this
    one, so it wins.
    """
    from jiuwenswarm.common import config as config_mod

    monkeypatch.setattr(
        config_mod,
        "get_config",
        lambda: {"clouddoc": {"enabled": True, "mode": "mandate"}},
    )
    yield
