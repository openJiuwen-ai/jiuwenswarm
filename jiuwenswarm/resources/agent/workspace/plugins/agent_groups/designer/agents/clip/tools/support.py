# -*- coding: utf-8 -*-
"""Shared checks for one member package. Imported while that package is on sys.path."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

_SHARED_MODULE = "designer_node_tools"


def args_of(inputs):
    if isinstance(inputs, dict):
        return inputs
    dump = getattr(inputs, "model_dump", None)
    if callable(dump):
        data = dump()
        if isinstance(data, dict):
            return data
    return {}


def ticket_of(data):
    ticket = data.get("shot_ticket")
    if not isinstance(ticket, str) or not ticket.strip():
        return None
    return ticket


def file_uri_ok(uri):
    if not isinstance(uri, str) or not uri.startswith("file:"):
        return False
    path = unquote(urlparse(uri).path or "")
    if len(path) >= 3 and path[0] == "/" and path[2] == ":":
        return True
    return path.startswith("/") and len(path) > 1


def node_tools():
    """The group's shared implementation, ``<group>/tools/node_tools.py``.

    Manifest tool files may not reach outside their member directory, so the
    shared module is loaded by path, once per process.
    """
    module = sys.modules.get(_SHARED_MODULE)
    if module is None:
        path = Path(__file__).resolve().parents[3] / "tools" / "node_tools.py"
        spec = importlib.util.spec_from_file_location(_SHARED_MODULE, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[_SHARED_MODULE] = module
    return module
