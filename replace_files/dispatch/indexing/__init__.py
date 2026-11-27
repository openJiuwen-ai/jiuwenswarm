"""Canonical offline indexing package."""

from .catalog.records import CatalogRecord
from .catalog.retrieval_text import build_embedding_record_text
from .models import (
    CATALOG_FILENAME,
    INDEX_MANIFEST_FILENAME,
    TREE_HTML_FILENAME,
    TREE_INDEX_FILENAME,
)

__all__ = [
    "CATALOG_FILENAME",
    "CatalogRecord",
    "INDEX_MANIFEST_FILENAME",
    "TREE_HTML_FILENAME",
    "TREE_INDEX_FILENAME",
    "build_embedding_record_text",
]