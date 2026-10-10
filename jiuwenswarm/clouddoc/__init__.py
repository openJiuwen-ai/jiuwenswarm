"""Co-scribe: mandate-governed co-editing of shared cloud documents.

Layered bottom-up: ``providers`` (platform contract and implementations),
``edits`` and ``receipts`` (the write rails), ``tools`` (what the agent calls),
``authority`` and ``state`` (what the watcher keeps), ``watch`` (the unattended
path), ``panel`` (the owner's operations), ``host`` (the only package that imports
the agent runtime or the gateway). ``tests/unit_tests/clouddoc/test_layering.py``
enforces the direction of every import.
"""
