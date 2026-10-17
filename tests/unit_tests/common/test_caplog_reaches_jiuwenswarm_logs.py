"""``caplog`` must see the ``jiuwenswarm`` logger tree, and only when asked for."""

from __future__ import annotations

import logging


def test_a_warning_from_the_jiuwenswarm_logger_reaches_caplog(caplog):
    """``setup_logger()`` turns propagation off on the ``jiuwenswarm`` logger at import
    time, and ``caplog`` listens on the root logger; without the fixture the record is
    printed to stderr and ``caplog.records`` stays empty."""
    with caplog.at_level(logging.WARNING, logger="jiuwenswarm"):
        logging.getLogger("jiuwenswarm.common.utils").warning("visible to the test")
    assert any(r.message == "visible to the test" for r in caplog.records)


def test_a_test_without_caplog_leaves_propagation_alone():
    """Propagation also feeds pytest's live-log handler, which reinstalls its own
    stderr over one a test has replaced; a test that never asks for ``caplog`` must
    not pay for it."""
    assert logging.getLogger("jiuwenswarm").propagate is False
