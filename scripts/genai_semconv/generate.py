# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Generate the Python and TypeScript OpenTelemetry GenAI constants."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml


SCRIPT_DIR = Path(__file__).resolve().parent
SWARM_ROOT = SCRIPT_DIR.parents[1]
VERSION_FILE = SCRIPT_DIR / "versions.json"
TS_OUTPUT = (
    SWARM_ROOT
    / "jiuwenswarm/channels/web/frontend/src/features/trajectory/semconv"
    / "gen-ai-semconv.generated.ts"
)
PY_OUTPUT_RELATIVE = Path("openjiuwen/extensions/observability/gen_ai_semconv.py")
REVISION_PATTERN = re.compile(r"[0-9a-f]{40}")


def _run(command: list[str], *, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return completed.stdout.strip()


def _load_yaml(path: Path) -> dict[str, Any]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"expected a YAML mapping in {path}")
    return loaded


def _load_versions() -> dict[str, str]:
    loaded = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
    required = {"repository", "revision", "weaver_version"}
    if not isinstance(loaded, dict) or not required.issubset(loaded):
        raise ValueError(f"invalid version lock file: {VERSION_FILE}")
    return {key: str(value) for key, value in loaded.items()}


def _weaver_command(version: str, registry_dir: Path) -> list[str]:
    local_weaver = shutil.which("weaver")
    normalized_version = version.removeprefix("v")
    if local_weaver:
        output = _run([local_weaver, "--version"])
        if re.search(rf"\b{re.escape(normalized_version)}\b", output):
            return [local_weaver]

    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError(
            f"Weaver {version} is not installed and Docker is unavailable"
        )
    return [
        docker,
        "run",
        "--rm",
        "-u",
        f"{os.getuid()}:{os.getgid()}",
        "-v",
        f"{registry_dir}:/workspace",
        "-w",
        "/workspace",
        "-e",
        "HOME=/tmp",
        f"otel/weaver:{version}",
    ]


def _resolve_registry(
    repository: str,
    revision: str,
    weaver_version: str,
    work_dir: Path,
) -> tuple[Path, Path]:
    registry_dir = work_dir / "registry"
    registry_dir.mkdir()
    _run(["git", "init", "--quiet"], cwd=registry_dir)
    _run(["git", "remote", "add", "origin", repository], cwd=registry_dir)
    _run(
        ["git", "fetch", "--quiet", "--depth", "1", "origin", revision],
        cwd=registry_dir,
    )
    _run(["git", "checkout", "--quiet", "FETCH_HEAD"], cwd=registry_dir)
    resolved_revision = _run(["git", "rev-parse", "HEAD"], cwd=registry_dir)
    if resolved_revision != revision:
        raise RuntimeError(
            f"resolved revision {resolved_revision} does not match requested {revision}"
        )

    manifest_path = registry_dir / "model/manifest.yaml"
    manifest = _load_yaml(manifest_path)
    schema_url = str(manifest["schema_url"])
    schema_version = schema_url.rstrip("/").rsplit("/", 1)[-1]
    resolved_schema_uri = (
        "https://github.com/open-telemetry/semantic-conventions-genai/"
        f"releases/download/v{schema_version}/resolved.yaml"
    )
    output_dir = registry_dir / ".build/package"
    weaver = _weaver_command(weaver_version, registry_dir)
    _run(
        [
            *weaver,
            "registry",
            "package",
            "-r",
            "./model",
            "--v2",
            "--resolved-registry-uri",
            resolved_schema_uri,
            "-o",
            "./.build/package",
        ],
        cwd=registry_dir,
    )
    return output_dir / "resolved.yaml", manifest_path


def _constant_name(attribute: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", attribute).strip("_").upper()


def _camel_case(value: str) -> str:
    first, *rest = value.split("_")
    return first + "".join(part.title() for part in rest)


def _extract_definitions(
    resolved_path: Path,
    manifest_path: Path,
) -> tuple[str, str, list[str], list[str]]:
    resolved = _load_yaml(resolved_path)
    manifest = _load_yaml(manifest_path)
    schema_url = str(resolved["schema_url"])
    dependencies = manifest.get("dependencies", [])
    core_schema_url = ""
    for dependency in dependencies:
        registry_path = str(dependency.get("registry_path", ""))
        if "open-telemetry/semantic-conventions" in registry_path:
            core_schema_url = str(dependency["schema_url"])
            break
    if not core_schema_url:
        raise ValueError(f"upstream semantic-conventions dependency missing from {manifest_path}")

    catalog = resolved.get("attribute_catalog")
    if not isinstance(catalog, list):
        raise ValueError(f"attribute_catalog missing from {resolved_path}")

    attributes = sorted(
        {
            str(item["key"])
            for item in catalog
            if isinstance(item, dict) and str(item.get("key", "")).startswith("gen_ai.")
        }
    )
    if not attributes:
        raise ValueError("no gen_ai.* attributes found in resolved registry")

    operations: list[str] = []
    for item in catalog:
        if not isinstance(item, dict) or item.get("key") != "gen_ai.operation.name":
            continue
        attribute_type = item.get("type")
        if not isinstance(attribute_type, dict):
            continue
        for member in attribute_type.get("members", []):
            if not isinstance(member, dict) or "value" not in member:
                continue
            value = str(member["value"])
            if value not in operations:
                operations.append(value)
    if not operations:
        raise ValueError("gen_ai.operation.name has no standard members")
    return schema_url, core_schema_url, attributes, operations


def _render_python(
    revision: str,
    schema_url: str,
    core_schema_url: str,
    attributes: list[str],
) -> str:
    exported = [
        "GEN_AI_SEMCONV_REVISION",
        "GEN_AI_SEMCONV_SCHEMA_URL",
        "GEN_AI_CORE_SEMCONV_SCHEMA_URL",
        "GEN_AI_SEMCONV_ATTRIBUTE_COUNT",
        *(_constant_name(attribute) for attribute in attributes),
    ]
    lines = [
        "# coding: utf-8",
        "# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.",
        "",
        '"""Generated OpenTelemetry GenAI semantic-convention attribute names.',
        "",
        "Source: open-telemetry/semantic-conventions-genai",
        f"Revision: {revision}",
        "",
        "This module contains only upstream standard definitions. Replace it as one",
        "unit when the pinned GenAI semantic-conventions registry is upgraded; project",
        "extensions belong in ``semconv.py``.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        "from typing import Final",
        "",
        "",
        f'GEN_AI_SEMCONV_REVISION: Final = "{revision}"',
        f'GEN_AI_SEMCONV_SCHEMA_URL: Final = "{schema_url}"',
        f'GEN_AI_CORE_SEMCONV_SCHEMA_URL: Final = "{core_schema_url}"',
        f"GEN_AI_SEMCONV_ATTRIBUTE_COUNT: Final = {len(attributes)}",
        "",
    ]
    lines.extend(
        f'{_constant_name(attribute)}: Final = "{attribute}"'
        for attribute in attributes
    )
    lines.extend(["", "__all__ = ("])
    lines.extend(f'    "{name}",' for name in exported)
    lines.extend([")", ""])
    return "\n".join(lines)


def _render_typescript(
    revision: str,
    schema_url: str,
    core_schema_url: str,
    attributes: list[str],
    operations: list[str],
) -> str:
    lines = [
        "// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.",
        "",
        "/**",
        " * Generated OpenTelemetry GenAI semantic-convention definitions.",
        " *",
        " * Source: open-telemetry/semantic-conventions-genai",
        f" * Revision: {revision}",
        " *",
        " * Replace this file as one unit when the pinned registry is upgraded. Product",
        " * extensions and compatibility aliases belong in constants.ts.",
        " */",
        "",
        f"export const GEN_AI_SEMCONV_REVISION = '{revision}' as const",
        f"export const GEN_AI_SEMCONV_SCHEMA_URL = '{schema_url}' as const",
        f"export const GEN_AI_CORE_SEMCONV_SCHEMA_URL = '{core_schema_url}' as const",
        f"export const GEN_AI_SEMCONV_ATTRIBUTE_COUNT = {len(attributes)} as const",
        "",
        "export const GEN_AI_ATTRIBUTES = {",
    ]
    lines.extend(
        f"  {_constant_name(attribute)}: '{attribute}'," for attribute in attributes
    )
    lines.extend(["} as const", "", "export const GEN_AI_OPERATIONS = {"])
    lines.extend(
        f"  {_camel_case(operation)}: '{operation}'," for operation in operations
    )
    lines.extend(["} as const", ""])
    return "\n".join(lines)


def _show_diff(path: Path, expected: str) -> bool:
    actual = path.read_text(encoding="utf-8") if path.exists() else ""
    if actual == expected:
        return False
    print(
        "".join(
            difflib.unified_diff(
                actual.splitlines(keepends=True),
                expected.splitlines(keepends=True),
                fromfile=str(path),
                tofile=f"{path} (generated)",
            )
        ),
        end="",
    )
    return True


def _write_atomic(path: Path, content: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)
    return True


def _write_revision(versions: dict[str, str], revision: str) -> None:
    if versions["revision"] == revision:
        return
    updated = {**versions, "revision": revision}
    _write_atomic(VERSION_FILE, json.dumps(updated, indent=2) + "\n")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--agent-core-dir",
        type=Path,
        default=SWARM_ROOT.parent / "agent-core",
        help="path to the agent-core checkout",
    )
    parser.add_argument(
        "--revision",
        help="40-character upstream commit; also updates versions.json on success",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail when generated outputs differ without writing files",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    versions = _load_versions()
    revision = args.revision or versions["revision"]
    if not REVISION_PATTERN.fullmatch(revision):
        raise ValueError("revision must be a lowercase 40-character Git commit")

    agent_core_dir = args.agent_core_dir.expanduser().resolve()
    python_output = agent_core_dir / PY_OUTPUT_RELATIVE
    if not (agent_core_dir / "pyproject.toml").is_file():
        raise FileNotFoundError(f"agent-core checkout not found: {agent_core_dir}")

    build_root = SWARM_ROOT / ".build"
    build_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="genai-semconv-", dir=build_root
    ) as temporary:
        resolved_path, manifest_path = _resolve_registry(
            versions["repository"],
            revision,
            versions["weaver_version"],
            Path(temporary),
        )
        schema_url, core_schema_url, attributes, operations = _extract_definitions(
            resolved_path,
            manifest_path,
        )

    outputs = {
        python_output: _render_python(
            revision,
            schema_url,
            core_schema_url,
            attributes,
        ),
        TS_OUTPUT: _render_typescript(
            revision,
            schema_url,
            core_schema_url,
            attributes,
            operations,
        ),
    }
    if args.check:
        changed = False
        for path, content in outputs.items():
            changed = _show_diff(path, content) or changed
        if changed:
            print("GenAI semantic-convention outputs are out of date.")
            return 1
        print(
            f"GenAI semantic-convention outputs are current: "
            f"{len(attributes)} attributes, {len(operations)} operations."
        )
        return 0

    changed_paths = [
        path for path, content in outputs.items() if _write_atomic(path, content)
    ]
    _write_revision(versions, revision)
    if changed_paths:
        print("Updated GenAI semantic-convention outputs:")
        for path in changed_paths:
            print(f"  {path}")
    else:
        print("GenAI semantic-convention outputs were already current.")
    print(f"Generated {len(attributes)} attributes and {len(operations)} operations.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
