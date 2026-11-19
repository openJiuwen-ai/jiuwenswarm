# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Full bundle validation remains required when expert teams launch."""

from pathlib import Path

import pytest

from openjiuwen.agent_teams.experts import load_agent_group_package_bundle


def test_load_bundle_missing_manifest_raises_value_error(tmp_path: Path) -> None:
    package = tmp_path / "missing-manifest"
    package.mkdir()
    with pytest.raises(ValueError, match="manifest.*missing"):
        load_agent_group_package_bundle(package)



def test_load_bundle_invalid_json_raises_value_error(tmp_path: Path) -> None:
    package = tmp_path / "bad-json"
    package.mkdir()
    (package / "manifest.json").write_text("{not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        load_agent_group_package_bundle(package)



def test_load_bundle_non_object_raises_value_error(tmp_path: Path) -> None:
    package = tmp_path / "list-manifest"
    package.mkdir()
    (package / "manifest.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="must contain a JSON object"):
        load_agent_group_package_bundle(package)
