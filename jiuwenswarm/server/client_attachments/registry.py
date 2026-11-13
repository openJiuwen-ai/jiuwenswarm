# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The client apps JiuwenSwarm ships (``APPS``): what a client may attach with, and what its conversations load.

A conversation belongs to the client app its ``session.create`` named
(``client_app``). Its agent loads the app's package directory as an agent
template (``manifest.json``: the app's persona, rail and skills). The tools
the app's client may declare at attach are the ``name``s in the ``tools.json``
of the same directory. Its agent is built without the capabilities the app
``leaves_out``. ``client_apps`` resolves each app into a ``ClientApp``;
``conversation_app`` is the app a conversation belongs to.
"""

from __future__ import annotations

import importlib.util
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from jiuwenswarm.server.client_attachments.errors import ClientAttachError, ClientAttachErrorCode


class Capability(StrEnum):
    """What a conversation's agent is built with that an app's conversations may leave out."""

    # Delegating to subagents: their prompt section and the subagent_* tools.
    SUBAGENTS = "subagents"
    # send_file_to_user.
    FILE_DELIVERY = "file_delivery"
    # ResponsePromptRail: the prompt sections on the message envelope, on final replies and file delivery, and A2UI.
    MESSAGE_RULES = "message_rules"


# An Office task pane's conversation: the document is the deliverable, the task pane renders no A2UI and the
# persona sets the message rules.
_TASK_PANE_LEAVES_OUT = frozenset({Capability.SUBAGENTS, Capability.FILE_DELIVERY, Capability.MESSAGE_RULES})

# client_app: the app's package directory, ``<package>/<relative dir>`` inside an installed Python package so it
# resolves the same from source and from a frozen bundle, and the capabilities its conversations leave out.
APPS: dict[str, tuple[str, frozenset[Capability]]] = {
    "office-powerpoint": ("jiuwenswarm/office/powerpoint", _TASK_PANE_LEAVES_OUT),
}


@dataclass(frozen=True)
class ClientApp:
    id: str
    package_dir: Path
    tool_names: frozenset[str]
    leaves_out: frozenset[Capability]


@lru_cache(maxsize=1)
def client_apps() -> dict[str, ClientApp]:
    """Every app JiuwenSwarm ships, by ``client_app``."""
    apps: dict[str, ClientApp] = {}
    for app_id, (package, leaves_out) in APPS.items():
        module, _, relative = package.partition("/")
        package_dir = Path(importlib.util.find_spec(module).submodule_search_locations[0]) / relative
        tools = json.loads((package_dir / "tools.json").read_text("utf-8"))
        apps[app_id] = ClientApp(id=app_id, package_dir=package_dir,
                                 tool_names=frozenset(tool["name"] for tool in tools), leaves_out=leaves_out)
    return apps


def registered_app(client_app: str) -> ClientApp:
    app = client_apps().get(client_app)
    if app is None:
        raise ClientAttachError(
            ClientAttachErrorCode.APP_UNAVAILABLE, f"no client app {client_app} is registered")
    return app


def conversation_app(metadata: Mapping[str, Any] | None) -> ClientApp | None:
    """The registered app whose client created the conversation (its metadata's ``client_app``), if any.

    Such a conversation shows only in its app's client.
    """
    return client_apps().get((metadata or {}).get("client_app") or "")
