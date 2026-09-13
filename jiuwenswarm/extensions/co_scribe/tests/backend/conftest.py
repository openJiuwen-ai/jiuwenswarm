"""Shared defaults for co-scribe's backend tests.

Both fixtures came with the tests when they moved into the plugin: one from the
agentserver tree's conftest, one from ``tests/conftest.py``. Neither is new
behaviour -- they are the environment these tests have always run in, carried
across so the move stays a move.
"""

from __future__ import annotations

import logging
from typing import Generator

import pytest

# The write-tool tests: the ones that used to live under
# ``tests/unit_tests/agentserver`` and were written before D16, in a world where
# the ask machinery exists. The watcher- and panel-side tests never had this
# default and must not acquire it here -- the panel does not call the write
# tools, it *reports* whether a channel exists, and its tests drive that answer
# from the config on purpose.
_WRITE_TOOL_TESTS = frozenset({
    "test_clouddoc_apply_direct.py",
    "test_clouddoc_authz.py",
    "test_clouddoc_formats.py",
    "test_clouddoc_provider.py",
    "test_clouddoc_provider_contract.py",
    "test_clouddoc_result_predicates.py",
    "test_clouddoc_routing.py",
    "test_clouddoc_structure.py",
    "test_clouddoc_toolkit.py",
    "test_feishu_provider.py",
})


@pytest.fixture(autouse=True)
def _ask_channel_present(request, monkeypatch):
    """Run every write-tool test in the attended world unless it says otherwise.

    D16 made the write tools consult the session's confirmation channel; the
    pre-D16 tests were all written against a world where the ask machinery exists,
    and re-stating that per test would be four dozen copies of one line. Tests
    that exercise the Full Access floor override this with ``False`` explicitly.
    """
    if request.path.name not in _WRITE_TOOL_TESTS:
        yield
        return

    import jiuwenswarm.extensions.co_scribe.backend.toolkit.clouddoc_tools as ct

    monkeypatch.setattr(ct, "_ask_channel_available", lambda: True)
    yield


@pytest.fixture(autouse=True)
def _jiuwenswarm_logs_reach_caplog() -> Generator[None, None, None]:
    """Let ``caplog`` see records from the ``jiuwenswarm`` logger tree.

    ``jiuwenswarm.common.utils`` calls ``setup_logger()`` at import time, which sets
    ``propagate = False`` on the ``jiuwenswarm`` logger and attaches its own handlers.
    ``caplog`` installs its handler on the *root* logger, so nothing ever reaches it
    and every ``caplog.records`` assertion sees an empty list -- the warning is emitted
    and visible on stderr, yet the test fails.

    ``tests/conftest.py`` does this for the tests under ``tests/``. These tests live
    beside the plugin now, outside that tree, so they carry their own copy.
    """
    logger = logging.getLogger("jiuwenswarm")
    previous = logger.propagate
    logger.propagate = True
    try:
        yield
    finally:
        logger.propagate = previous


@pytest.fixture(autouse=True)
def _clouddoc_config_is_not_the_developers(monkeypatch):
    """No test result may depend on the machine's own ``config.yaml``.

    ``CloudDocCommentWatcher.tick`` reads ``clouddoc.enabled`` and ``clouddoc.mode``
    live on every poll, deliberately: uninstalling the plugin flips the flag
    mid-session and the loop has to notice without a restart. Nothing stubbed that in
    the tests, so the whole watcher suite silently inherited whatever the developer
    had configured.

    It is not hypothetical. With ``enabled: false`` sitting in a real workspace, 57
    tests failed at once with "nothing was dispatched" -- and pointed at a rebase that
    had nothing to do with it, because the code under test was byte-identical either
    side. A test that answers differently on two machines is worse than a missing
    test: it accuses the wrong change.

    A deployment with the feature on, in the only mode that has an unattended path.
    A test that needs another mode patches ``get_config`` itself, and that patch is
    applied after this one, so it wins.
    """
    from jiuwenswarm.common import config as config_mod

    monkeypatch.setattr(
        config_mod,
        "get_config",
        lambda: {"clouddoc": {"enabled": True, "mode": "mandate"}},
    )
    yield
