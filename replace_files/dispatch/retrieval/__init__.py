"""Canonical online retrieval package.

Keep package import lightweight to avoid eager initialization of the full
retrieval stack during compatibility imports.
"""

__all__ = ["io", "lexical", "merge", "protocols", "semantic", "service", "tree"]
