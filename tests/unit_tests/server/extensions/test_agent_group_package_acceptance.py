# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Black-box fixture coverage for the AgentGroup package acceptance CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
import importlib.util
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "acceptance" / "agent_group_package_acceptance.py"
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "agent_groups" / "swarmflow-expert-fixture"


def _load_acceptance_module():
    spec = importlib.util.spec_from_file_location(
        "agent_group_package_acceptance", SCRIPT
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(*args: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    env = dict(os.environ)
    process = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    stdout_lines = [line for line in process.stdout.splitlines() if line.strip()]
    assert stdout_lines, process.stderr
    return process, json.loads(stdout_lines[-1])


def test_validate_only_uses_production_loader_and_reports_each_case(
    tmp_path: Path,
) -> None:
    invalid = tmp_path / "missing-manifest"
    invalid.mkdir()

    process, report = _run(
        "validate-only",
        str(FIXTURE),
        str(invalid),
        "--expected-count",
        "2",
    )

    assert process.returncode == 1
    assert report["operations"] == ["load_agent_group_package_bundle"]
    assert report["cases"][0] == {
        "source": str(FIXTURE),
        "passed": True,
        "packageId": "swarmflow-expert-fixture",
        "memberCount": 2,
        "members": ["leader", "echo"],
    }
    assert report["cases"][1]["passed"] is False
    assert "manifest" in report["cases"][1]["error"]


def test_rootless_zip_import_visible_in_list_show_and_installed_storage(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "swarmflow-expert-fixture.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for path in FIXTURE.rglob("*"):
            if path.is_file():
                bundle.write(path, path.relative_to(FIXTURE))

    data_dir = tmp_path / "clean-data"
    report_path = tmp_path / "report.json"
    process, report = _run(
        "import-local",
        str(archive),
        "--data-dir",
        str(data_dir),
        "--expected-count",
        "1",
        "--json-out",
        str(report_path),
    )

    assert process.returncode == 0, process.stderr
    assert report["passed"] is True
    assert report["operations"][:3] == [
        "agent_groups.import_local",
        "agent_groups.list",
        "agent_groups.show",
    ]
    case = report["cases"][0]
    assert case["packageId"] == "swarmflow-expert-fixture"
    assert case["sourceType"] == "archive"
    assert case["memberCount"] == 2
    assert case["show"]["id"] == "swarmflow-expert-fixture"
    assert case["show"]["memberCount"] == 2
    assert case["show"]["avatar"]["present"] is False
    assert case["show"]["members"] == [
        {
            "id": "leader",
            "role": "leader",
            "avatar": {"present": False, "kind": "none", "length": 0},
        },
        {
            "id": "echo",
            "role": "member",
            "avatar": {"present": False, "kind": "none", "length": 0},
        },
    ]
    assert case["show"]["skillIds"] == []
    assert case["list"] == case["show"]
    assert {key: case["show"][key] for key in ("source", "installed", "canUse")} == {
        "source": "local",
        "installed": True,
        "canUse": True,
    }
    assert case["memberSkillIds"] == {"leader": [], "echo": []}
    assert Path(case["installedPath"], "manifest.json").is_file()
    assert report["catalog"]["missingImportedIds"] == []
    assert json.loads(report_path.read_text(encoding="utf-8"))["passed"] is True


@pytest.mark.parametrize(
    ("field", "value", "expected_fragment"),
    [
        ("source", "marketplace", "source='marketplace'"),
        ("installed", False, "installed=False"),
        ("canUse", False, "canUse=False"),
        ("memberCount", 1, "memberCount=1"),
    ],
)
def test_catalog_contract_rejects_unusable_or_inconsistent_cards(
    field: str,
    value: object,
    expected_fragment: str,
) -> None:
    module = _load_acceptance_module()
    card = {
        "id": "fixture",
        "source": "local",
        "installed": True,
        "memberCount": 2,
        "capabilities": {"canUse": True},
    }
    if field == "canUse":
        card["capabilities"]["canUse"] = value
    else:
        card[field] = value

    with pytest.raises(ValueError, match="contract mismatch") as exc_info:
        module._assert_catalog_card_contract(
            card,
            package_id="fixture",
            expected_member_count=2,
            surface="agent_groups.list",
        )

    assert expected_fragment in str(exc_info.value)


def test_setup_failure_preserves_submitted_package_count_and_paths(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "does-not-exist.zip"

    process, report = _run("validate-only", str(missing))

    assert process.returncode == 1
    assert report["packageCount"] == 1
    assert report["packagePaths"] == [str(missing)]
    assert report["cases"] == []
    assert str(missing) in report["errors"][0]
