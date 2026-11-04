# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The lightweight reset preserves the original Auto-Harness file contract."""

import json

from jiuwenswarm.agents.harness.common.auto_harness.state import reset_harness_packages_state


def test_reset_only_changes_existing_activation_fields(tmp_path):
    path = tmp_path / "harness-packages.json"
    path.write_text(json.dumps({
        "active_package_ids": ["active"],
        "active_package_id": "active",
        "active_extension_name": "legacy",
        "native_version": {"id": "native", "is_active": False},
        "packages": [{"id": "inactive"}, {"id": "active", "is_active": True}],
        "other": "preserved",
    }), encoding="utf-8")
    reset_harness_packages_state(path)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "active_package_ids": [],
        "native_version": {"id": "native", "is_active": True},
        "packages": [{"id": "inactive"}, {"id": "active", "is_active": False}],
        "other": "preserved",
    }
    previous = path.read_bytes()
    reset_harness_packages_state(path)
    assert path.read_bytes() == previous
