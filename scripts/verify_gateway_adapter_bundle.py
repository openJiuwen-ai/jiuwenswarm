# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Verify Gateway imports and physical extension resources in the frozen backend."""

import argparse
import logging
import os
import sys
import subprocess
from importlib import import_module
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory


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
    verify_gateway_extensions()


def run_isolated(executable: Path) -> int:
    """Set paths before the frozen entrypoint can import or cache user settings."""
    executable = executable.resolve()
    script = Path(__file__).resolve()
    with TemporaryDirectory(prefix="gateway-bundle-check-") as directory:
        root = Path(directory).resolve()
        (root / "config").mkdir()
        env = os.environ.copy()
        env["JIUWENSWARM_DATA_DIR"] = str(root)
        env["JIUWENSWARM_HOME"] = str(root)
        env["AGENTOS_GATEWAY_LOG_DIR"] = str(root / "logs")
        env.pop("JIUWENSWARM_RUNTIME_WORKSPACE_READY", None)
        result = subprocess.run(
            [str(executable), str(script)],
            cwd=root,
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            check=False,
        )
        # The child has exited, including entrypoint failure handling. Its file
        # handles are closed before the parent cleans up the isolated directory.
        if result.returncode:
            logging.getLogger(__name__).error("Frozen Gateway verification failed (exit %s)", result.returncode)
            for log in sorted(root.rglob("*.log")):
                logging.getLogger(__name__).error("%s", log.read_text(encoding="utf-8", errors="replace")[-16000:])
        return result.returncode


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if getattr(sys, "frozen", False):
        verify_gateway_adapter_bundle()
        logging.getLogger(__name__).info("Gateway adapter frozen bundle verification passed")
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", required=True, type=Path)
    args = parser.parse_args()
    return run_isolated(args.executable)


if __name__ == "__main__":
    raise SystemExit(main())
