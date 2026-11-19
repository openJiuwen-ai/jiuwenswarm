# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The client apps JiuwenSwarm ships (``APPS``): what a client may attach with, and what its conversations load.

A conversation belongs to the client app its ``session.create`` named
(``client_app``). Its agent loads the app's package directory as an agent
template (``manifest.json``: the app's persona, rail and skills). The tools
the app's client may declare at attach are the tools in the ``tools.json`` of
the same directory whose ``host_requirements`` name the app's platform. Its
agent is built without the capabilities the app ``leaves_out``.
``client_apps`` resolves each app into a ``ClientApp``; ``conversation_app``
is the app a conversation belongs to.
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

from jiuwenswarm.office.contract import PLATFORMS
from jiuwenswarm.server.client_attachments.errors import ClientAttachError, ClientAttachErrorCode


class Capability(StrEnum):
    """What a conversation's agent is built with that an app's conversations may leave out."""

    # Delegating to subagents: their prompt section and the subagent_* tools.
    SUBAGENTS = "subagents"
    # send_file_to_user.
    FILE_DELIVERY = "file_delivery"
    # ResponsePromptRail: the prompt sections on the message envelope, on final replies and file delivery, and A2UI.
    MESSAGE_RULES = "message_rules"
    # The workspace's skill library; the app's own skills come with its template.
    SKILL_LIBRARY = "skill_library"


# An Office task pane's conversation: the document is the deliverable, the task pane renders no A2UI, the
# persona sets the message rules and the template brings the skills (a library skill would build files instead).
_TASK_PANE_LEAVES_OUT = frozenset({
    Capability.SUBAGENTS, Capability.FILE_DELIVERY, Capability.MESSAGE_RULES, Capability.SKILL_LIBRARY})

# client_app: the app's package directory, ``<package>/<relative dir>`` inside an installed Python package so it
# resolves the same from source and from a frozen bundle, the platform its host is of, and the capabilities its
# conversations leave out. A platform's apps share a package with another platform's app for the same documents.
APPS: dict[str, tuple[str, str, frozenset[Capability]]] = {
    "office-powerpoint": ("jiuwenswarm/office/powerpoint", "office", _TASK_PANE_LEAVES_OUT),
    "office-word": ("jiuwenswarm/office/word", "office", _TASK_PANE_LEAVES_OUT),
    "office-excel": ("jiuwenswarm/office/excel", "office", _TASK_PANE_LEAVES_OUT),
    "wps-presentation": ("jiuwenswarm/office/powerpoint", "wps", _TASK_PANE_LEAVES_OUT),
}


def _check_platforms() -> None:
    unknown = sorted(app for app, (_, platform, _) in APPS.items() if platform not in PLATFORMS)
    if unknown:
        raise ValueError(f"client apps {unknown} name a platform outside {sorted(PLATFORMS)}")


_check_platforms()


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
    for app_id, (package, platform, leaves_out) in APPS.items():
        module, _, relative = package.partition("/")
        package_dir = Path(importlib.util.find_spec(module).submodule_search_locations[0]) / relative
        tools = json.loads((package_dir / "tools.json").read_text("utf-8"))
        apps[app_id] = ClientApp(id=app_id, package_dir=package_dir,
                                 tool_names=frozenset(tool["name"] for tool in tools
                                                      if platform in tool["host_requirements"]),
                                 leaves_out=leaves_out)
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
