from __future__ import annotations

import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SYMPHONY_ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def dispatch_import_path() -> Iterator[None]:
    """Expose the vendored dispatch source as top-level packages."""

    root = str(SYMPHONY_ROOT)
    inserted = False
    if root not in sys.path:
        sys.path.append(root)
        inserted = True
    try:
        yield
    finally:
        if inserted:
            try:
                sys.path.remove(root)
            except ValueError:
                pass
