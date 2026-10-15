"""Package native sources and static resources; no Node at install/runtime."""
import hashlib
import json
import shutil
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = Path(__file__).parent
DIRECTORIES = ("backends/macos/Sources", "backends/windows", "assets/agent-pointer", "skills")
FILES = ("backends/macos/Package.swift",)
SUFFIXES = (".swift", ".cs", ".py", ".svg", ".json", ".md", ".html", ".css", ".js", ".mjs")


class BuildWithResources(build_py):

    def run(self):
        super().run()
        output = Path(self.build_lib) / "computer_use/_resources"
        # A reused build directory must not ship sources removed in a later
        # revision. This directory is generated entirely by this command.
        if output.exists():
            shutil.rmtree(output)
        # Drivers are optional source components. The common SDK can be built
        # before a platform driver is installed or merged into the checkout.
        sources = [ROOT / name for name in FILES if (ROOT / name).is_file()]
        for directory in DIRECTORIES:
            directory_root = ROOT / directory
            for path in directory_root.rglob("*"):
                if not path.is_file():
                    continue
                parts = path.relative_to(directory_root).parts
                if any(part.startswith(".") or part == "__pycache__" for part in parts):
                    continue
                if path.suffix in SUFFIXES:
                    sources.append(path)
        manifest = {}
        for source in sorted(sources):
            relative = source.relative_to(ROOT)
            target = output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            manifest[relative.as_posix()] = hashlib.sha256(source.read_bytes()).hexdigest()
        content = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        (output / "manifest.json").write_text(content, encoding="utf-8")


setup(cmdclass={"build_py": BuildWithResources})
