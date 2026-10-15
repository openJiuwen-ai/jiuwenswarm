"""Ship the independently buildable Computer Use runtime in WorkSwarm wheels."""

import hashlib
import importlib.util
import json
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

module_spec = importlib.util.spec_from_file_location(
    "computer_use_bundle",
    Path(__file__).parent / "jiuwenswarm/agents/harness/computer_use/bundle.py",
)
bundle = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(bundle)


class BuildWithComputerUse(build_py):

    def run(self):
        super().run()
        root = Path(__file__).parent / "jiuwenswarm" / "cua_engine"
        output = Path(self.build_lib) / "jiuwenswarm" / "cua_engine"
        if output.exists():
            shutil.rmtree(output)
        # The adapter and engine are independently mergeable components. Build
        # ordinary WorkSwarm packages until the optional engine is present.
        if not (root / "pyproject.toml").is_file():
            return
        manifest = {}
        for path in bundle.runtime_files(root):
            relative = path.relative_to(root)
            destination = output / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
            manifest[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        (output / "manifest.json").write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")


setup(cmdclass={"build_py": BuildWithComputerUse})
