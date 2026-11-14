import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIN_PROTOBUF5_COMPATIBLE_OTEL = Version("1.28.0")
OTEL_REQUIREMENTS = {
    "opentelemetry-api",
    "opentelemetry-sdk",
    "opentelemetry-exporter-otlp-proto-grpc",
    "opentelemetry-exporter-otlp-proto-http",
}


def test_opentelemetry_dependencies_exclude_protobuf4_only_proto_versions():
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    requirements = {
        Requirement(raw).name: Requirement(raw)
        for raw in pyproject["project"]["dependencies"]
    }

    for name in OTEL_REQUIREMENTS:
        specifier = requirements[name].specifier
        assert specifier.contains(MIN_PROTOBUF5_COMPATIBLE_OTEL), (
            f"{name} must allow OpenTelemetry {MIN_PROTOBUF5_COMPATIBLE_OTEL}"
        )
        assert not specifier.contains(Version("1.27.0")), (
            f"{name} must exclude OpenTelemetry 1.27.0 and older because "
            "opentelemetry-proto<1.28 requires protobuf<5"
        )


def test_core_pin_matches_pip_uv_and_lockfile():
    """All installation paths must select the same immutable adapted Core."""
    import re

    project = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    lock = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text())
    regular = next(Requirement(raw) for raw in project["project"]["dependencies"]
                   if Requirement(raw).name == "openjiuwen")
    harmony = next(Requirement(raw) for raw in project["project"]["optional-dependencies"]["harmony"]
                   if Requirement(raw).name == "openjiuwen")
    source = project["tool"]["uv"]["sources"]["openjiuwen"]
    revision = source["rev"]
    assert re.fullmatch(r"[0-9a-f]{40}", revision)
    assert regular.url == harmony.url == f"git+{source['git']}@{revision}"
    core = next(package for package in lock["package"] if package["name"] == "openjiuwen")
    assert core["source"]["git"] == f"{source['git']}?rev={revision}#{revision}"
    assert core["version"] == "0.1.20"
    sdk = next(package for package in lock["package"] if package["name"] == "adx-environment")
    requirement = Requirement(project["project"]["optional-dependencies"]["adx"][0])
    assert requirement.specifier.contains(sdk["version"], prereleases=True)
