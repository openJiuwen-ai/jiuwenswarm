# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""统一资产错误码单测。"""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.assets.errors import (
    AssetError,
    AssetErrorCode,
    asset_http_error_status,
    map_internal_error,
)


def test_contract_codes_are_fixed_eight() -> None:
    """契约错误码为固定八项。"""
    assert AssetErrorCode.ALL == {
        "ASSET_NOT_FOUND",
        "ASSET_EXPORT_FORBIDDEN",
        "ASSET_INVALID_PACKAGE",
        "ASSET_UNSAFE_PATH",
        "ASSET_CREDENTIAL_DETECTED",
        "ASSET_LIMIT_EXCEEDED",
        "ASSET_ALREADY_EXISTS",
        "ASSET_BUILTIN_READ_ONLY",
    }
    # 类常量与 ALL 一一对应，防止后续加常量漏进 ALL（或反之）
    constants = {
        getattr(AssetErrorCode, name)
        for name in vars(AssetErrorCode)
        if name.isupper() and name != "ALL"
    }
    assert constants == AssetErrorCode.ALL


@pytest.mark.parametrize(
    "code", ["ASSET_WHATEVER", "NOT_FOUND", "ASSET_NOT_FOUND ", "", "INTERNAL_ERROR"]
)
def test_asset_error_rejects_non_contract_code(code) -> None:
    with pytest.raises(ValueError):
        AssetError(code, "boom")


def test_asset_error_payload_shape() -> None:
    err = AssetError(AssetErrorCode.ALREADY_EXISTS, "my-expert already exists")
    assert err.to_payload() == {
        "error": "my-expert already exists",
        "code": "ASSET_ALREADY_EXISTS",
    }

    err = AssetError(
        AssetErrorCode.NOT_FOUND, "no such skill", details={"kind": "skill", "local_id": "x"}
    )
    payload = err.to_payload()
    assert payload["code"] == "ASSET_NOT_FOUND"
    assert payload["details"] == {"kind": "skill", "local_id": "x"}

    # 缺省 message 回退为错误码本身，避免 error 字段为空
    assert AssetError(AssetErrorCode.LIMIT_EXCEEDED).to_payload()["error"] == (
        "ASSET_LIMIT_EXCEEDED"
    )


class _FakePackageBuildError(ValueError):
    """仿 marketplace PackageBuildError 的形状（.code 实例属性）。"""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@pytest.mark.parametrize(
    ("internal_code", "expected"),
    [
        ("UNSAFE_PATH", "ASSET_UNSAFE_PATH"),
        ("OUTPUT_INSIDE_SOURCE", "ASSET_UNSAFE_PATH"),
        ("LIMIT_EXCEEDED", "ASSET_LIMIT_EXCEEDED"),
        ("CREDENTIAL_VALUE", "ASSET_CREDENTIAL_DETECTED"),
        ("INVALID_PACKAGE", "ASSET_INVALID_PACKAGE"),
    ],
)
def test_map_internal_error_known_codes(internal_code, expected) -> None:
    mapped = map_internal_error(_FakePackageBuildError(internal_code))
    assert isinstance(mapped, AssetError)
    assert mapped.code == expected
    assert mapped.details == {"internal_code": internal_code}


@pytest.mark.parametrize(
    "exc",
    [
        _FakePackageBuildError("SOURCE_CHANGED"),  # 表外内部码 → 入口 INTERNAL_ERROR 兜底
        ValueError("plain"),  # 无 .code
        RuntimeError(),
        AssetError(AssetErrorCode.NOT_FOUND, "契约异常应被入口透传，不走映射"),
    ],
)
def test_map_internal_error_unmapped(exc) -> None:
    assert map_internal_error(exc) is None


def test_map_internal_error_maps_real_package_build_error() -> None:
    """映射基于 .code 属性，对 marketplace 真实的 PackageBuildError 同样生效。"""
    from jiuwenswarm.server.runtime.marketplace.asset_package_builder import (
        PackageBuildError,
    )

    mapped = map_internal_error(PackageBuildError("LIMIT_EXCEEDED"))
    assert mapped is not None
    assert mapped.code == AssetErrorCode.LIMIT_EXCEEDED


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("ASSET_NOT_FOUND", 404),
        ("ASSET_EXPORT_FORBIDDEN", 403),
        ("ASSET_INVALID_PACKAGE", 400),
        ("ASSET_UNSAFE_PATH", 400),
        ("ASSET_CREDENTIAL_DETECTED", 400),
        ("ASSET_LIMIT_EXCEEDED", 413),
        ("ASSET_ALREADY_EXISTS", 409),
        ("ASSET_BUILTIN_READ_ONLY", 403),
        # 入口层码：内部故障显式 500，参数错误与未知码按 400 兜底
        ("INTERNAL_ERROR", 500),
        ("INVALID_PARAMS", 400),
        ("ASSET_FUTURE_CODE", 400),
    ],
)
def test_asset_http_error_status(code, expected) -> None:
    assert asset_http_error_status(code) == expected
