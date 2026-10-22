"""The provider contract: what every platform implementation must declare.

The checks that run each implementation against the contract move with the
implementations, and the check that every load-bearing capability flag has a reader
in production code moves with the wiring PR, once the readers are in this package.
This one concerns the abstract base alone.
"""
from __future__ import annotations


from jiuwenswarm.clouddoc.providers.base import DocProvider


def test_the_abstract_base_still_declares_what_it_used_to():
    """A method quietly dropped from DocProvider would take its implementations'
    obligation with it, and nothing would fail.
    """
    for name in ("read", "edit_batch", "list_comments", "reply_comment", "capabilities"):
        assert hasattr(DocProvider, name), f"DocProvider 少了 {name}"
