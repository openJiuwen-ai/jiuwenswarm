"""The deployment's cloud-document feature flag, and the plugin's only setting.

``clouddoc.enabled`` in ``config.yaml`` is the single authority for co-scribe.
The plugin's ``is_enabled()`` reads it, which decides whether the Docs page is
contributed to the nav at all and what the 应用插件 card's badge says; the
gateway's watcher, the polling loop and both tool factories read the same key.
Nothing keeps a second copy of the answer, so the card, the rail and the
feature cannot disagree.

``get_config`` re-parses ``config.yaml`` whenever the file's mtime or size
changes, so a flip written here -- or by installing/uninstalling the co-scribe
plugin package -- is visible to the next manifest request without a restart.
"""

from __future__ import annotations

from typing import Any

import jiuwenswarm.common.config as config

CLOUDDOC_SECTION = "clouddoc"
DEFAULT_MODE = "mandate"
# The modes the gateway understands; anything else is repaired on enable rather
# than left to fail later in the watcher.
VALID_MODES = ("mandate", "recorded", "direct")


def clouddoc_enabled() -> bool:
    """Return the deployment's ``clouddoc.enabled`` flag.

    A missing key reads as off, matching the shipped ``config.yaml`` default and
    the gate in front of both tool factories.
    """

    # Read through the module rather than a bound name so a test that swaps the
    # config layer -- and the lifecycle test that already does -- reaches this.
    section = config.get_config().get(CLOUDDOC_SECTION) or {}
    return bool(section.get("enabled"))


def set_clouddoc_enabled(enabled: bool) -> None:
    """Write ``clouddoc.enabled``, repairing the mode when turning the feature on.

    Connections, watch grants and the receipt ledger are untouched either way --
    turning co-scribe off hides its surfaces and stops its turns, it does not
    discard what the deployment configured.
    """

    def mutate(data: dict[str, Any]) -> dict[str, Any]:
        section = data.setdefault(CLOUDDOC_SECTION, {})
        section["enabled"] = enabled
        if enabled and str(section.get("mode") or "").strip().lower() not in VALID_MODES:
            section["mode"] = DEFAULT_MODE
        return data

    config.update_config(mutate)


def settings_payload() -> dict[str, Any]:
    """The payload both settings RPCs answer with."""

    return {"enabled": clouddoc_enabled()}
