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
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as stream:
        pyproject = tomllib.load(stream)
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


def test_sdk_dependency_and_lock_use_the_same_immutable_revision():
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as stream:
        pyproject = tomllib.load(stream)
    with (PROJECT_ROOT / "uv.lock").open("rb") as stream:
        lock = tomllib.load(stream)

    sdk_source = pyproject["tool"]["uv"]["sources"]["openjiuwen"]
    revision = sdk_source["rev"]
    assert len(revision) == 40 and all(char in "0123456789abcdef" for char in revision)
    git_url = sdk_source["git"]
    dependency_groups = [pyproject["project"]["dependencies"]]
    dependency_groups.extend(pyproject["project"]["optional-dependencies"].values())
    sdk_urls = [
        requirement.url
        for group in dependency_groups
        for raw in group
        if (requirement := Requirement(raw)).name == "openjiuwen" and requirement.url
    ]
    assert sdk_urls and set(sdk_urls) == {f"git+{git_url}@{revision}"}

    locked_sdk = [package for package in lock["package"] if package["name"] == "openjiuwen"]
    assert len(locked_sdk) == 1
    assert locked_sdk[0]["source"] == {"git": f"{git_url}?rev={revision}#{revision}"}

    project = next(
        package for package in lock["package"]
        if package["name"] == pyproject["project"]["name"]
    )
    sdk_metadata = [
        requirement for requirement in project["metadata"]["requires-dist"]
        if requirement["name"] == "openjiuwen"
    ]
    assert sdk_metadata
    assert all(requirement.get("git") == f"{git_url}?rev={revision}" for requirement in sdk_metadata)
