"""Workswarm-authored bootstrap for Cutter 2.5.0 on Windows and macOS.

The portable Cutter 2.5.0 build creates Python plugin objects but does not
complete the CutterPlugin lifecycle callbacks. REA's bridge can start without
the MainWindow argument, so this wrapper starts it during construction and
keeps the normal setupInterface callback idempotent if a later Cutter build
calls it.

This file is not shipped by REA or Cutter. It is a compatibility shim owned by
Workswarm and exists only for:
https://github.com/rizinorg/cutter/issues/3659
https://github.com/rizinorg/cutter/pull/3674

The actual REA bridge remains the unmodified rea_bridge.py imported below.
"""

from __future__ import annotations

import atexit

import rea_bridge


class WorkswarmReaCutterPlugin(rea_bridge.ReaCutterPlugin):
    """REA bridge with a Cutter 2.5.0 lifecycle workaround."""

    def __init__(self) -> None:
        super().__init__()
        atexit.register(self.terminate)
        self.setupInterface(None)

    def setupInterface(self, main: object) -> None:
        if self._bridge is not None:
            return
        super().setupInterface(main)


def create_cutter_plugin() -> WorkswarmReaCutterPlugin:
    return WorkswarmReaCutterPlugin()
