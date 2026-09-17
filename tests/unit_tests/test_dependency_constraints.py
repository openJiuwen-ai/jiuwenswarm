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
