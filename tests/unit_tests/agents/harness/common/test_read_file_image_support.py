# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""跨版本 ReadFileTool 图片能力接线：锁定 core 无 resolver 参数时不得 TypeError。

``uv.lock`` 锁定的 agent-core（8215f2c）只有 ``enable_image_multimodal`` 静态参数；
agent-core 的 ``image_multimodal_resolver`` 尚未合入 dev-stable。这里验证兼容层：
- 探测结果与实际 ``ReadFileTool.__init__`` 签名一致；
- core 不支持时关键字参数里只有静态 bool（构造不炸，退回构造时求值）；
- core 支持时把 resolver 透传下去（合入后自动启用调用时求值）。
"""

from __future__ import annotations

import inspect

import pytest
from openjiuwen.harness.tools.filesystem import ReadFileTool

from jiuwenswarm.agents.harness.common import read_file_image_support as support


def test_support_flag_matches_installed_core_signature() -> None:
    parameters = inspect.signature(ReadFileTool.__init__).parameters
    expected = support.IMAGE_MULTIMODAL_RESOLVER_PARAM in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    assert support.supports_read_file_image_resolver() is expected


def test_kwargs_omit_resolver_when_core_is_locked(monkeypatch) -> None:
    monkeypatch.setattr(support, "_supports_resolver", False)
    kwargs = support.build_read_file_image_kwargs(
        lambda: True, enable_image_multimodal=False
    )
    assert kwargs == {"enable_image_multimodal": False}


def test_kwargs_pass_resolver_when_core_supports(monkeypatch) -> None:
    monkeypatch.setattr(support, "_supports_resolver", True)
    def resolver() -> bool:
        return False

    kwargs = support.build_read_file_image_kwargs(
        resolver, enable_image_multimodal=True
    )
    assert kwargs["enable_image_multimodal"] is True
    assert kwargs[support.IMAGE_MULTIMODAL_RESOLVER_PARAM] is resolver


@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        (type("LegacyTool", (), {"enable_image_multimodal": False})(), False),
        (type("LegacyTool", (), {"enable_image_multimodal": True})(), True),
    ],
    ids=["legacy-off", "legacy-on"],
)
def test_enabled_reader_falls_back_to_instance_attribute(tool, expected) -> None:
    """旧 core 没有 ``_image_multimodal_enabled()`` 时读静态属性。"""

    assert support.read_file_image_multimodal_enabled(tool) is expected