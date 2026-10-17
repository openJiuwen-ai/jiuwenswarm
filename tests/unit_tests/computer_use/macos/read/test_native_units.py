"""Compile and run native read/protocol units without sending desktop input."""
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


TESTS = Path(__file__).resolve().parent
ENGINE = TESTS.parents[4] / "jiuwenswarm/cua_engine"
SOURCES = ENGINE / "backends/macos/Sources"
UNITS = sorted(path.stem.removesuffix("Tests") for path in TESTS.glob("*Tests.swift"))
FULL_MODULE = {
    "ApplicationRegistration", "NativeMenuSurfaces", "NativeSelectionBasis",
    "DriverDispatch", "NativeExecutionContract",
}
EXTRA = {
    "AccessibilityActivation": ["NativeProcessIdentity"],
    "ObservationBudget": ["Protocol", "NativeMetrics"],
    "NativeOperation": [
        "RequestLedger", "Protocol", "NativeMetrics", "ObservationBudget", "NativeTarget", "NativeIntent",
    ],
    "NativeTarget": ["Protocol", "NativeMetrics", "ObservationBudget"],
    "CapturedImage": ["Protocol", "NativeMetrics", "ObservationBudget"],
    "NativeWindowBoundary": ["AppSelection"],
    "AXRelationReader": ["NativeRelationPages", "AXFactEncoding"],
    "LocalMenuAttachment": ["ObservedPath"],
    "AXIdentity": ["AXRead", "ElementIdentity"],
}
WIRE_CASES = {
    "NativeInspectQuery": ["native-inspect-queries.json"],
    "NativeUnitCommand": ["native-unit-commands.json", "native-begin-requests.json"],
    "NativeCaptureRequest": ["native-capture-requests.json"],
}


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swiftc") is None,
    reason="Requires macOS and the Swift compiler",
)
@pytest.mark.parametrize("name", UNITS)
def test_native_unit(name, tmp_path):
    sources = sorted(SOURCES.rglob("*.swift"))
    if name in FULL_MODULE:
        inputs = [path for path in sources if path.name != "main.swift"]
    else:
        dependencies = [] if name == "Failure" else ["Failure"]
        if name in WIRE_CASES:
            dependencies += ["Protocol", "NativeMetrics", "ObservationBudget", "NativeTarget"]
            if name == "NativeUnitCommand":
                dependencies.append("NativeIntent")
        dependencies += EXTRA.get(name, []) + [name]
        inputs = []
        for dependency in dependencies:
            matches = [path for path in sources if path.stem == dependency]
            assert len(matches) == 1, f"Expected one production source for {dependency}: {matches}"
            inputs.append(matches[0])
    executable = tmp_path / name
    compiled = subprocess.run(
        ["swiftc", "-swift-version", "6", "-parse-as-library", *map(str, inputs),
         str(TESTS / f"{name}Tests.swift"), "-o", str(executable)],
        cwd=ENGINE, capture_output=True, text=True, timeout=120,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    cases = [str(TESTS / "fixtures" / case) for case in WIRE_CASES.get(name, [])]
    result = subprocess.run(
        [str(executable), *cases], cwd=ENGINE, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
