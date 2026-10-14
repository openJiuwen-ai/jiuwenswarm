"""Package native sources and static resources; no Node at install/runtime."""
import hashlib
import json
from pathlib import Path
import shutil
from setuptools import setup
from setuptools.command.build_py import build_py

ROOT=Path(__file__).parent
DIRECTORIES=("backends/macos/Sources", "backends/windows", "assets/agent-pointer", "skills")
FILES=("backends/macos/Package.swift",)


class BuildWithResources(build_py):
    def run(self):
        super().run()
        output=Path(self.build_lib)/"computer_use/_resources"
        # A reused build directory must not ship sources removed in a later
        # revision. This directory is generated entirely by this command.
        if output.exists():
            shutil.rmtree(output)
        sources=[ROOT/name for name in FILES]
        for directory in DIRECTORIES:
            sources.extend(path for path in (ROOT/directory).rglob("*") if path.is_file() and not any(part.startswith(".") or part=="__pycache__" for part in path.relative_to(ROOT/directory).parts)
                           and path.suffix in (".swift",".cs",".py",".svg",".json",".md",".html",".css",".js",".mjs"))
        manifest={}
        for source in sorted(sources):
            relative=source.relative_to(ROOT);target=output/relative
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(source,target)
            manifest[relative.as_posix()]=hashlib.sha256(source.read_bytes()).hexdigest()
        content=json.dumps(manifest,sort_keys=True,separators=(",",":"))
        (output/"manifest.json").write_text(content,encoding="utf-8")


setup(cmdclass={"build_py":BuildWithResources})
