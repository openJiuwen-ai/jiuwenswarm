# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""统一资产错误码与入口收敛映射。

八个 ``ASSET_*`` 码是导入导出链路唯一的契约错误码：各模块内部异常必须在
``assets.export`` / ``assets.import`` 入口层收敛至此，前端据此映射多语言
Toast；禁止各 kind 实现私自扩充错误码（:class:`AssetError` 构造时校验）。

映射策略：

- :class:`AssetError` —— 适配器/服务层直接抛出的契约异常，入口原样透传；
- ``SafeArchiveError`` / ``PackageBuildError`` 等带 ``.code`` 的内部异常 ——
  经 :func:`map_internal_error` 查表收敛（按 ``.code`` 属性识别，避免
  errors 模块反向依赖 common.safe_archive 之外的实现包）；
- 其余异常 —— 不属于八类业务拒绝，由入口 handler 按全仓既有约定以
  ``INTERNAL_ERROR`` 兜底（表示服务端内部故障，可重试/可上报）。
"""

from __future__ import annotations

from typing import Any


class AssetErrorCode:
    """标准错误码 —— 字符串常量，与前端映射表逐字一致。"""

    NOT_FOUND = "ASSET_NOT_FOUND"  # 资产或指定版本不存在
    EXPORT_FORBIDDEN = "ASSET_EXPORT_FORBIDDEN"  # 未安装或受限资产不可导出
    INVALID_PACKAGE = "ASSET_INVALID_PACKAGE"  # 缺少 SKILL.md / manifest.json 或包结构非法
    UNSAFE_PATH = "ASSET_UNSAFE_PATH"  # 包含符号链接（symlink）或路径穿越
    CREDENTIAL_DETECTED = "ASSET_CREDENTIAL_DETECTED"  # 模板文件包含明文敏感凭证
    LIMIT_EXCEEDED = "ASSET_LIMIT_EXCEEDED"  # 文件数、单文件或压缩包体积超限
    ALREADY_EXISTS = "ASSET_ALREADY_EXISTS"  # 目标 <id> 已存在（禁止覆盖）
    BUILTIN_READ_ONLY = "ASSET_BUILTIN_READ_ONLY"  # 目标 <id> 与系统内置资产冲突（禁止覆盖）

    ALL = frozenset(
        {
            NOT_FOUND,
            EXPORT_FORBIDDEN,
            INVALID_PACKAGE,
            UNSAFE_PATH,
            CREDENTIAL_DETECTED,
            LIMIT_EXCEEDED,
            ALREADY_EXISTS,
            BUILTIN_READ_ONLY,
        }
    )


class AssetError(Exception):
    """携带标准错误码的业务异常。

    ``to_payload()`` 输出 ``{"error": message, "code": code}``，与现有 WS
    handler 的错误形状一致，入口层可直接作为 ``AgentResponse.payload``。
    """

    def __init__(
        self,
        code: str,
        message: str = "",
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        if code not in AssetErrorCode.ALL:
            raise ValueError(f"not a contract asset error code: {code!r}")
        self.code = code
        self.message = message or code
        self.details = dict(details or {})
        super().__init__(self.message)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": self.message, "code": self.code}
        if self.details:
            payload["details"] = dict(self.details)
        return payload


# 安全打包层（common.safe_archive.SafeArchiveError，marketplace 侧别名
# PackageBuildError）的内部码 → 契约码。其 code 是实例属性而非异常类型，
# 按码查表收敛。不在表内的内部码（如 SOURCE_CHANGED：打包期间源目录发生
# 变化，属可重试的内部故障而非业务拒绝）由入口层以 INTERNAL_ERROR 兜底。
_PACKAGE_BUILD_CODE_MAP: dict[str, str] = {
    "UNSAFE_PATH": AssetErrorCode.UNSAFE_PATH,
    "OUTPUT_INSIDE_SOURCE": AssetErrorCode.UNSAFE_PATH,
    "LIMIT_EXCEEDED": AssetErrorCode.LIMIT_EXCEEDED,
    "CREDENTIAL_VALUE": AssetErrorCode.CREDENTIAL_DETECTED,
    # 损坏、加密、不支持的压缩包与非 UTF-8 包内容：确定性的无效资产包，
    # 不得落入 INTERNAL_ERROR 兜底，否则调用方无法与可重试故障区分。
    "INVALID_PACKAGE": AssetErrorCode.INVALID_PACKAGE,
}


# ASSET_* 契约码 → HTTP 状态码。供 /file-api/assets/* HTTP 面使用；
# WS 入口不走该映射，直接以 ok=False + to_payload() 返回错误。
_HTTP_STATUS: dict[str, int] = {
    AssetErrorCode.NOT_FOUND: 404,
    AssetErrorCode.EXPORT_FORBIDDEN: 403,
    AssetErrorCode.INVALID_PACKAGE: 400,
    AssetErrorCode.UNSAFE_PATH: 400,
    AssetErrorCode.CREDENTIAL_DETECTED: 400,
    AssetErrorCode.LIMIT_EXCEEDED: 413,
    AssetErrorCode.ALREADY_EXISTS: 409,
    AssetErrorCode.BUILTIN_READ_ONLY: 403,
    # 入口层内部故障码（全仓既有约定）：服务端故障显式 500，
    # 不得落未知码 400 兜底伪装成客户端错误。
    "INTERNAL_ERROR": 500,
}


def asset_http_error_status(code: str) -> int:
    """错误码 → HTTP 状态码；INTERNAL_ERROR 显式 500，未知码（如 INVALID_PARAMS）按 400 兜底。"""
    return _HTTP_STATUS.get(code, 400)


def asset_http_error_body(code: str, message: str) -> dict[str, str]:
    """HTTP 错误响应体，与 /file-api/skills/* 的 {code, message, error} 形状一致。"""
    return {"code": code, "message": message, "error": message}


def map_internal_error(exc: BaseException) -> AssetError | None:
    """把带 ``.code`` 的内部异常映射为契约错误码；无法映射时返回 None。

    :class:`AssetError` 的 ``.code`` 是契约码、不在内部码映射表内，本函数
    对其返回 None。入口层的收敛顺序：``AssetError`` 以 isinstance 判定后
    原样透传 → 本函数查表映射 → 未命中时以 INTERNAL_ERROR 兜底。
    """
    internal_code = getattr(exc, "code", None)
    if not isinstance(internal_code, str):
        return None
    mapped = _PACKAGE_BUILD_CODE_MAP.get(internal_code)
    if mapped is None:
        return None
    return AssetError(
        mapped,
        str(exc) or internal_code,
        details={"internal_code": internal_code},
    )


def converge_error_payload(exc: BaseException) -> dict[str, Any]:
    """把任意异常收敛为入口响应 payload（``{"error", "code"}`` 形状）。

    收敛顺序（WS handler 与 WS 适配器两条入口共用，保证同错同码）：

    1. :class:`AssetError` —— 契约异常原样透传；
    2. 带 ``.code`` 的内部异常 —— :func:`map_internal_error` 查表收敛；
    3. 无内部码的 ``ValueError`` —— 契约层参数校验失败，``INVALID_PARAMS``
       （与 session_assets、extensions_v2.management 等一致的入口层参数
       错误码，不属于八类业务契约码，前端落通用失败提示）；
    4. 其余 —— ``INTERNAL_ERROR`` 兜底。
    """
    if isinstance(exc, AssetError):
        return exc.to_payload()
    mapped = map_internal_error(exc)
    if mapped is not None:
        return mapped.to_payload()
    if isinstance(exc, ValueError) and not hasattr(exc, "code"):
        return {"error": str(exc) or "invalid params", "code": "INVALID_PARAMS"}
    return {"error": str(exc) or "internal error", "code": "INTERNAL_ERROR"}
