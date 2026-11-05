# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Verify Gateway imports and physical extension resources in the frozen backend."""

import argparse
import logging
import os
import sys
import subprocess
from importlib import import_module
from importlib.resources import files


def verify_gateway_extensions() -> None:
    """Exercise the package's file-based loader without starting extensions."""
    package = import_module("agent_gateway")
    import_module("agent_gateway.app_gateway")
    version("agent-gateway")
    extension_loader = import_module("agent_gateway.extensions.loader").ExtensionLoader

    class VerificationLoader(extension_loader):
        """Expose import-only verification without registering or starting plugins."""

        @classmethod
        def import_entry(cls, root: Path):
            return cls._import_module(root)

    loader = VerificationLoader
    extension_dir = Path(package.__file__).resolve().parent / "extensions"
    # These built-ins belong to the pinned Gateway dependency. Checking names
    # explicitly prevents an empty/missing directory from passing verification.
    for name in ("adx", "agentos", "agent_client"):
        root = extension_dir / name
        for filename in ("extension.yaml", "extension.py"):
            if not (root / filename).is_file():
                raise RuntimeError(f"Missing frozen Gateway extension resource: {root / filename}")
        if not loader.load_manifest(root):
            raise RuntimeError(f"Empty frozen Gateway extension manifest: {root}")
        module = loader.import_entry(root)
        if not callable(getattr(module, "register_extensions", None)):
            raise RuntimeError(f"Missing Gateway extension registration entry: {root}")


def verify_gateway_adapter_bundle() -> None:
    """Fail if a lazy adapter implementation or its imports was not frozen."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Gateway adapter verification must run inside the frozen executable")
    gateway_adapter = import_module("jiuwenswarm.server.runtime.gateway_adapter")
    for export_name in gateway_adapter.__all__:
        getattr(gateway_adapter, export_name)
    # The Front port can be ready before this lazy backend import succeeds.
    for module_name in (
        "jiuwenswarm.server.agent_ws_server",
        "agent_gateway.app_gateway",
        "agent_gateway.channel_manager.im_platforms.welink.welink_runtime",
        "openjiuwen.harness.persistent_agent",
        "openjiuwen_runtime.foundation.security.link_mtls_config",
        "jiuwenswarm.server.jiuwenmate.agent_mail_local_provider",
        "jiuwenswarm.server.jiuwenmate.dingtalk_cli_local_provider",
    ):
        import_module(module_name)
    extension_root = files("agent_gateway.extensions")
    for extension in ("adx", "agent_client", "agentos"):
        for name in ("extension.yaml", "extension.py"):
            if not extension_root.joinpath(extension, name).is_file():
                raise RuntimeError(f"Missing frozen Gateway extension resource: {extension}/{name}")


if __name__ == "__main__":
    raise SystemExit(main())
