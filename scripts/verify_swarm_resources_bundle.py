# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Verify built-in tool discovery and message catalogs in the frozen backend."""

import argparse
import logging
import os
import subprocess
import sys
from importlib import import_module
from pathlib import Path
from tempfile import TemporaryDirectory


def verify_swarm_resources_bundle() -> None:
    """Check resources whose absence silently disables user-facing features."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Swarm resources verification must run inside the frozen executable")
    tools = import_module("jiuwenswarm.agents.harness.common.tools")
    registry = import_module("jiuwenswarm.agents.harness.common.tools.builtin_tool_registry")
    discovered = {spec.name for spec in registry.discover_builtin_tool_specs()}
    if "diff_files" not in discovered or "diff_files" not in tools.__all__:
        raise RuntimeError("Frozen built-in tool discovery is missing diff_files")

    translate = import_module("jiuwenswarm.common.i18n.translate")
    catalog = Path(translate.__file__).resolve().parent / "catalog"
    for language in ("en", "zh"):
        if not (catalog / f"{language}.yaml").is_file():
            raise RuntimeError(f"Missing frozen i18n catalog: {language}")
        message_id = "tools.send_file.sent"
        if translate.t(message_id, language=language, count=2) == message_id:
            raise RuntimeError(f"Frozen i18n translation failed: {language}")


def run_isolated(executable: Path) -> int:
    """Isolate configuration before the frozen entrypoint initializes paths."""
    executable = executable.resolve()
    script = Path(__file__).resolve()
    with TemporaryDirectory(prefix="swarm-resources-check-") as directory:
        root = Path(directory).resolve()
        (root / "config").mkdir()
        env = os.environ.copy()
        env.update({
            "JIUWENSWARM_DATA_DIR": str(root),
            "JIUWENSWARM_HOME": str(root),
            "AGENTOS_GATEWAY_LOG_DIR": str(root / "logs"),
        })
        env.pop("JIUWENSWARM_RUNTIME_WORKSPACE_READY", None)
        result = subprocess.run(
            [str(executable), str(script)], cwd=root, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            check=False,
        )
        if result.returncode:
            logging.error("Frozen Swarm resources verification failed (exit %s)", result.returncode)
            for log in sorted(root.rglob("*.log")):
                logging.error("%s", log.read_text(encoding="utf-8", errors="replace")[-16000:])
        return result.returncode


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if getattr(sys, "frozen", False):
        verify_swarm_resources_bundle()
        logging.info("Swarm resources frozen bundle verification passed")
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", required=True, type=Path)
    return run_isolated(parser.parse_args().executable)


if __name__ == "__main__":
    raise SystemExit(main())
