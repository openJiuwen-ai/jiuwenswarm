# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Keep extension package persistence inside each test's temporary workspace."""

import pytest


@pytest.fixture(autouse=True)
def isolate_package_store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "jiuwenswarm.extensions_v2.assembly.resolve_store_dir",
        lambda: tmp_path / "package-data",
    )
