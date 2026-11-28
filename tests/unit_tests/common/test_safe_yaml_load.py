# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Both safe loader implementations reject Python object construction."""

from io import StringIO

import pytest
import yaml

from jiuwenswarm.common.yaml_utils import safe_yaml_load


@pytest.mark.parametrize("prefer_c", [True, False])
def test_safe_yaml_loader_rejects_python_constructors(monkeypatch, prefer_c):
    if not prefer_c:
        monkeypatch.delattr(yaml, "CSafeLoader", raising=False)
    with pytest.raises(yaml.constructor.ConstructorError):
        safe_yaml_load("!!python/object/apply:builtins.eval ['1 + 1']")


@pytest.mark.parametrize("prefer_c", [True, False])
def test_safe_yaml_loader_accepts_text_streams_and_preserves_caller_ownership(monkeypatch, prefer_c):
    if not prefer_c:
        monkeypatch.delattr(yaml, "CSafeLoader", raising=False)
    stream = StringIO("enabled: true\nvalues: [1, 2]\n")
    assert safe_yaml_load(stream) == {"enabled": True, "values": [1, 2]}
    assert not stream.closed
