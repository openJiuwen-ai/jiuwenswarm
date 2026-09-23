# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""华为云 IAM 逐请求签名 Provider（SSE 任务下发链路）。

背景（relay-claw 6.5 缺口）：探活链路（API 进程内 signedFetch）签名正常，但任务
下发链路把签名描述符（auth descriptor）发给本机 sidecar（jiuwenswarm）后，
openjiuwen 的远程 MCP 客户端只读 ``auth_headers`` / ``auth_query_params``，
描述符被静默丢弃——Agent 会话中 SSE 建流 GET 与消息 POST 均不带签名头，强制
签名的真实端点（如 lake-agent）401/403，且连接状态仍是 connected（探活走的是
API 进程内签名），缺口只能靠运行时验证工具发现。

本模块实现描述符消费侧（与 packages/api/src/domains/mcp-connectors/
request-mcp-servers-builder.ts 的 ``HuaweiCloudIamAuthDescriptor`` 契约一致）：

- ``parse_huawei_cloud_iam_auth``：解析/校验下发描述符；配合 ``create_mcp_tool``
  收进 ``McpServerConfig.params["_huawei_cloud_iam"]`` 随 worker params 流转。
- ``HuaweiCloudIamAuthProvider``（``httpx.Auth``）：挂在 mcp SDK
  ``sse_client`` / ``streamablehttp_client`` 的 ``auth=`` 上，按每个请求的实际
  method+URL 现算 SDK-HMAC-SHA256 / V11-HMAC-SHA256 签名（体不入签，
  preSigned 口径，与探活路径一致）；401 + APIG.0301（IAMv5 不被端点接受）时
  用 ``v3_credential`` 降级重签一次（对本连接粘性，后续请求直接用 v3 签）。
- ``apply_mcp_iam_signing_patch``：进程启动时打补丁（幂等），把描述符从
  ``McpServerConfig.params`` 布线进 SDK 工厂的 ``auth=``——不改 openjiuwen /
  mcp SDK 安装源码，venv 重装不丢失。

签名算法与 packages/api/src/utils/huawei-signed-request.ts 逐行对齐（V11 移植
自 ApiGateway-python-sdk-2.0.7/apig_sdk/signer_v11.py）。凭证强制目标域名
白名单（myhuaweicloud.com + env 扩展），不会签给第三方地址。
"""

from __future__ import annotations

import contextvars
import hashlib
import hmac
import os
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Mapping, Optional
from urllib.parse import parse_qsl, quote, unquote

import httpx

from openjiuwen.core.common.logging import logger

ALGORITHM_V1 = "SDK-HMAC-SHA256"
ALGORITHM_V11 = "V11-HMAC-SHA256"
_ALGORITHMS = frozenset({ALGORITHM_V1, ALGORITHM_V11})
#: V11 签名的 credential scope 服务名（对齐 apig_sdk signer_v11.py）。
_V11_SERVICE = "apic"
#: 预签名口径：请求体不入签，规范请求 payload 哈希填 UNSIGNED-PAYLOAD，
#: 同时发送 ``X-Sdk-Content-Sha256: UNSIGNED-PAYLOAD`` 让网关跳过请求体校验。
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
_SIGNED_HEADER_NAMES = "x-sdk-content-sha256;x-sdk-date"
#: 描述符在 ``McpServerConfig.params`` 内的携带键（``_`` 前缀 = 内部参数，
#: 与 ``_mcp_client_type`` 同口径；经 connect_params / _build_remote_mcp_config
#: 原样流转到 worker 重建的 client）。
IAM_AUTH_PARAMS_KEY = "_huawei_cloud_iam"

#: 默认 IAM 签名目标域名白名单：签名凭证是用户自己的华为云会话 AK/SK，
#: 只能签给华为云端点。可用 env 扩展（本地验证服务器等）。
_DEFAULT_ALLOWED_DOMAINS = ("myhuaweicloud.com",)


class IamSigningDomainNotAllowedError(RuntimeError):
    """IAM 签名描述符的目标域名不在白名单内（凭证不会签给第三方地址）。"""


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hmac_sha256_hex(secret: str, value: str) -> str:
    return hmac.new(secret.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def _format_sdk_date(now: Optional[datetime] = None) -> str:
    """``YYYYMMDDTHHMMSSZ``（UTC），与探活路径的 X-Sdk-Date 格式一致。"""
    dt = now or datetime.now(timezone.utc)
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _encode_rfc3986(value: str) -> str:
    """与 TS ``encode``（encodeURIComponent + !'()* 大写十六进制转义）等价。"""
    return quote(value, safe="")


def _canonical_uri(url: httpx.URL) -> str:
    """规范化 URI：逐段 decode→再 encode，缺尾部斜杠补齐（对齐探活实现）。"""
    path = url.path or "/"
    encoded = "/".join(_encode_rfc3986(unquote(part)) for part in path.split("/"))
    if not encoded.endswith("/"):
        encoded += "/"
    return encoded


def _canonical_query(url: httpx.URL) -> str:
    """规范化 query：decode 后按 (key, value) 排序再 encode。"""
    raw_query = bytes(url.query).decode("utf-8", errors="replace")
    if not raw_query:
        return ""
    pairs = parse_qsl(raw_query, keep_blank_values=True)
    pairs.sort(key=lambda kv: (kv[0], kv[1]))
    return "&".join(f"{_encode_rfc3986(k)}={_encode_rfc3986(v)}" for k, v in pairs)


def _build_canonical_request(method: str, url: httpx.URL, sdk_date: str) -> str:
    canonical_headers = f"x-sdk-content-sha256:{UNSIGNED_PAYLOAD}\nx-sdk-date:{sdk_date}\n"
    return "\n".join(
        [
            method.upper(),
            _canonical_uri(url),
            _canonical_query(url),
            canonical_headers,
            _SIGNED_HEADER_NAMES,
            UNSIGNED_PAYLOAD,
        ]
    )


def _hkdf_derive_key_v11(access_key: str, secret_key: str, credential_scope: str) -> str:
    """HKDF（RFC 5869）派生 V11 签名密钥，返回 hex（对齐 apig_sdk signer_v11）。"""
    prk = hmac.new(access_key.encode("utf-8"), secret_key.encode("utf-8"), hashlib.sha256).digest()
    expand_input = credential_scope.encode("utf-8") + b"\x01"
    okm = hmac.new(prk, expand_input, hashlib.sha256).digest()[:32]
    return okm.hex()


def _build_authorization_v1(
    method: str,
    url: httpx.URL,
    sdk_date: str,
    access_key: str,
    secret_key: str,
) -> str:
    canonical_request = _build_canonical_request(method, url, sdk_date)
    string_to_sign = "\n".join([ALGORITHM_V1, sdk_date, _sha256_hex(canonical_request)])
    signature = _hmac_sha256_hex(secret_key, string_to_sign)
    return (
        f"{ALGORITHM_V1} Access={access_key}, "
        f"SignedHeaders={_SIGNED_HEADER_NAMES}, Signature={signature}"
    )


def _build_authorization_v11(
    method: str,
    url: httpx.URL,
    sdk_date: str,
    access_key: str,
    secret_key: str,
    region_id: str,
) -> str:
    canonical_request = _build_canonical_request(method, url, sdk_date)
    credential_scope = f"{sdk_date[:8]}/{region_id}/{_V11_SERVICE}"
    string_to_sign = "\n".join(
        [ALGORITHM_V11, sdk_date, credential_scope, _sha256_hex(canonical_request)]
    )
    derived_key = _hkdf_derive_key_v11(access_key, secret_key, credential_scope)
    signature = _hmac_sha256_hex(derived_key, string_to_sign)
    return (
        f"{ALGORITHM_V11} Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={_SIGNED_HEADER_NAMES}, Signature={signature}"
    )


def _required_str(raw: Mapping[str, Any], key: str, *, context: str) -> str:
    value = raw.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ValueError(f"华为云 IAM 签名描述符{context}缺少有效字段 {key!r}")


def _optional_str(raw: Mapping[str, Any], key: str) -> str:
    value = raw.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def parse_huawei_cloud_iam_auth(raw: Any) -> Optional[dict[str, Any]]:
    """解析并校验下发的 IAM 签名描述符。

    Returns:
        ``None``：未下发（``raw is None``），调用方按无签名处理。
        规范化 dict：字段齐全（algorithm/access_key/secret_key/security_token/
        project_id/region_id/v3_credential），secret 仅驻留进程内。

    Raises:
        ValueError: 下发了描述符但格式/字段非法——必须显式失败，不允许静默
            丢弃后以未签名请求打真实端点（6.5 缺口的根因）。
    """
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("华为云 IAM 签名描述符必须是 JSON 对象")

    kind = _optional_str(raw, "kind")
    if kind != "huawei_cloud_iam":
        raise ValueError(f"不支持的 IAM 签名描述符 kind: {kind!r}（期望 huawei_cloud_iam）")

    algorithm = _optional_str(raw, "algorithm") or ALGORITHM_V1
    if algorithm not in _ALGORITHMS:
        raise ValueError(f"不支持的 IAM 签名算法: {algorithm!r}")

    descriptor: dict[str, Any] = {
        "kind": "huawei_cloud_iam",
        "algorithm": algorithm,
        "access_key": _required_str(raw, "access_key", context=""),
        "secret_key": _required_str(raw, "secret_key", context=""),
        "security_token": _required_str(raw, "security_token", context=""),
        "project_id": _optional_str(raw, "project_id"),
        "region_id": _optional_str(raw, "region_id"),
        "v3_credential": None,
    }
    if algorithm == ALGORITHM_V11 and not descriptor["region_id"]:
        raise ValueError("V11-HMAC-SHA256 签名描述符缺少 region_id")

    raw_v3 = raw.get("v3_credential")
    if raw_v3 is not None:
        if not isinstance(raw_v3, Mapping):
            raise ValueError("华为云 IAM 签名描述符的 v3_credential 必须是 JSON 对象")
        descriptor["v3_credential"] = {
            "access_key": _required_str(raw_v3, "access_key", context="（v3_credential）"),
            "secret_key": _required_str(raw_v3, "secret_key", context="（v3_credential）"),
            "security_token": _required_str(raw_v3, "security_token", context="（v3_credential）"),
        }
    return descriptor


def allowed_iam_signing_domains(environ: Optional[Mapping[str, str]] = None) -> tuple[str, ...]:
    """IAM 签名目标域名白名单：myhuaweicloud.com + env 扩展（逗号分隔）。

    env 名与 relay-claw API 侧 ``OFFICE_CLAW_CUSTOM_IAM_ALLOWED_DOMAINS`` 一致
    （sidecar spawn env 已透传），另支持 jiuwenswarm 自有名
    ``JIUWENSWARM_IAM_SIGN_ALLOWED_DOMAINS`` 优先覆盖。
    """
    env = os.environ if environ is None else environ
    raw = (
        env.get("JIUWENSWARM_IAM_SIGN_ALLOWED_DOMAINS")
        or env.get("OFFICE_CLAW_CUSTOM_IAM_ALLOWED_DOMAINS")
        or ""
    )
    extra = [item.strip().lower() for item in raw.split(",") if item.strip()]
    domains: list[str] = []
    for domain in (*_DEFAULT_ALLOWED_DOMAINS, *extra):
        if domain not in domains:
            domains.append(domain)
    return tuple(domains)


def _host_matches_allowed_domain(host: str, allowed_domains: tuple[str, ...]) -> bool:
    host = (host or "").strip().lower()
    if not host:
        return False
    return any(
        host == domain or host.endswith(f".{domain}") for domain in allowed_domains
    )


async def _response_mentions_apig_0301(response: httpx.Response) -> bool:
    """401 响应体是否为 APIG.0301（端点不接受 IAMv5，需要 v3 降级重签）。

    仅在 401 时读响应体（SSE 建流 200 的无限流绝不会被读取）；文本包含匹配
    覆盖 JSON ``error_code`` 与网关 HTML/XML 报错两种形态。
    """
    try:
        await response.aread()
    except Exception:  # noqa: BLE001 — 读失败按非 APIG.0301 处理，不改写 401 语义
        return False
    try:
        return "APIG.0301" in response.text
    except Exception:  # noqa: BLE001 — 解码失败同上
        return False


class HuaweiCloudIamAuthProvider(httpx.Auth):
    """华为云 IAM 逐请求签名 Provider（挂在 mcp SDK 工厂的 ``auth=`` 上）。

    对 SSE 建流 GET 与每条消息 POST 各自按实际 method+URL 现算签名
    （preSigned 口径：体不入签，``X-Sdk-Content-Sha256: UNSIGNED-PAYLOAD``，
    SignedHeaders=x-sdk-content-sha256;x-sdk-date）；401+APIG.0301 时切换
    ``v3_credential`` 重签一次并粘性降级。

    Args:
        descriptor: 下发的签名描述符（``parse_huawei_cloud_iam_auth`` 输入形态，
            构造时会再校验一次）。
        inner: 原有静态 ``httpx.Auth``（如 ``AuthHeaderAndQueryProvider``，用户
            额外头/查询参数）。签名前先套用 inner，保证 inner 合入的 query
            参数参与规范请求计算。inner 必须是「改请求后单次 yield」形态。
        allowed_domains: 目标域名白名单；缺省读 env（见
            ``allowed_iam_signing_domains``）。域名不匹配时抛
            ``IamSigningDomainNotAllowedError``——凭证绝不签给第三方地址。
    """

    def __init__(
        self,
        descriptor: Any,
        *,
        inner: Optional[httpx.Auth] = None,
        allowed_domains: Optional[tuple[str, ...]] = None,
    ) -> None:
        normalized = parse_huawei_cloud_iam_auth(descriptor)
        if normalized is None:
            raise ValueError("HuaweiCloudIamAuthProvider 缺少签名描述符")
        self._descriptor = normalized
        self._inner = inner
        self._allowed_domains = (
            allowed_iam_signing_domains() if allowed_domains is None else allowed_domains
        )
        self._use_v3 = False

    @property
    def algorithm(self) -> str:
        return str(self._descriptor["algorithm"])

    def _active_credential(self) -> dict[str, str]:
        v3 = self._descriptor.get("v3_credential")
        if self._use_v3 and v3:
            return {
                "access_key": v3["access_key"],
                "secret_key": v3["secret_key"],
                "security_token": v3["security_token"],
                "project_id": self._descriptor.get("project_id") or "",
            }
        return {
            "access_key": self._descriptor["access_key"],
            "secret_key": self._descriptor["secret_key"],
            "security_token": self._descriptor["security_token"],
            "project_id": self._descriptor.get("project_id") or "",
        }

    async def _apply_inner(self, request: httpx.Request) -> httpx.Request:
        """先套用静态 inner（用户额外头/查询参数），再计算签名。"""
        if self._inner is None:
            return request
        flow = self._inner.async_auth_flow(request)
        try:
            try:
                mutated = await flow.__anext__()
            except StopAsyncIteration:
                return request
            return mutated
        finally:
            await flow.aclose()

    def _sign_request(self, request: httpx.Request) -> None:
        sdk_date = _format_sdk_date()
        credential = self._active_credential()
        # httpx.Headers 大小写不敏感 set：同名不同大小写的旧值会被替换，
        # 服务端重算规范请求不会遇到 "value, value" 合并（对齐 TS 语义）。
        request.headers["X-Sdk-Date"] = sdk_date
        request.headers["X-Security-Token"] = credential["security_token"]
        request.headers["X-Sdk-Content-Sha256"] = UNSIGNED_PAYLOAD
        if credential["project_id"]:
            request.headers["X-Project-ID"] = credential["project_id"]
        if self.algorithm == ALGORITHM_V11:
            authorization = _build_authorization_v11(
                request.method,
                request.url,
                sdk_date,
                credential["access_key"],
                credential["secret_key"],
                self._descriptor["region_id"],
            )
        else:
            authorization = _build_authorization_v1(
                request.method,
                request.url,
                sdk_date,
                credential["access_key"],
                credential["secret_key"],
            )
        request.headers["Authorization"] = authorization

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        request = await self._apply_inner(request)
        host = str(request.url.host or "")
        if not _host_matches_allowed_domain(host, self._allowed_domains):
            raise IamSigningDomainNotAllowedError(
                f"华为云 IAM 签名目标域名不在白名单内: {host!r} "
                f"（允许: {', '.join(self._allowed_domains)}）；凭证不会签给第三方地址"
            )
        self._sign_request(request)
        response = yield request
        if response.status_code != 401 or self._use_v3:
            return
        if not self._descriptor.get("v3_credential"):
            return
        if not await _response_mentions_apig_0301(response):
            return
        # APIG.0301：端点不接受 IAMv5 临时凭证——切换 v3_credential 重签一次；
        # 粘性降级，本连接后续请求直接用 v3 签（401 意味着请求未被处理，重发安全）。
        logger.info(
            "[mcp-iam-signing] 401 APIG.0301 on %s; re-signing with v3 credential",
            str(request.url),
        )
        self._use_v3 = True
        self._sign_request(request)
        yield request


# ─── 进程级补丁：把描述符布线进 mcp SDK 工厂的 auth= ───
# （不改 openjiuwen / mcp SDK 安装源码，venv 重装不丢失；模式对齐
#   mcp_call_timeout_patch：包装 + 转发，各自幂等。）

_PATCHED = False
#: (module/class, name) 已包装目标——幂等哨兵，独立于 _PATCHED。
_wrapped_targets: set[tuple[object, str]] = set()
#: client 建连期间持有的描述符：connect 包装层 set，SDK 工厂包装层读取后
#: 换成签名 Provider（与 mcp_call_timeout_patch 的 _pending_sdk_read_timeout
#: 同一 contextvar 模式；SseClient 在 owner-task 的 _do_connect 里建 transport，
#: 与工厂调用同 task，contextvar 可见）。
_pending_iam_descriptor: contextvars.ContextVar[Optional[dict[str, Any]]] = (
    contextvars.ContextVar("_jws_pending_iam_descriptor", default=None)
)


def _descriptor_from_config(config: Any) -> Optional[dict[str, Any]]:
    """从 ``McpServerConfig.params`` 里取描述符（防御性再校验，坏值丢弃并告警）。"""
    params = getattr(config, "params", None)
    if not isinstance(params, Mapping):
        return None
    if params.get(IAM_AUTH_PARAMS_KEY) is None:
        return None
    try:
        return parse_huawei_cloud_iam_auth(params.get(IAM_AUTH_PARAMS_KEY))
    except ValueError as exc:
        logger.warning(
            "[mcp-iam-signing] 丢弃非法 IAM 签名描述符（server=%s）: %s",
            getattr(config, "server_name", "?"),
            exc,
        )
        return None


def _wrap_client_init(cls: type) -> None:
    """包装远程 MCP client ``__init__``：把 params 里的描述符 stamp 到实例上。

    覆盖所有构造路径（ToolMgr._create_client / 发现 / worker 重建），比在
    mcp_config 的两个构造点各贴一次更稳（未来新增构造点不会漏）。
    """
    if (cls, "__init__") in _wrapped_targets:
        return
    _wrapped_targets.add((cls, "__init__"))
    orig_init = cls.__init__

    def init_with_iam_stamp(self: Any, *args: Any, **kwargs: Any) -> None:
        orig_init(self, *args, **kwargs)
        config = args[0] if args else kwargs.get("config")
        try:
            setattr(self, "_jws_iam_descriptor", _descriptor_from_config(config))
        except Exception as exc:  # noqa: BLE001 — __slots__ 等拒绝动态属性时仅放弃 stamp
            logger.debug(
                "[mcp-iam-signing] failed to stamp descriptor on %s: %r",
                type(self).__name__,
                exc,
            )

    setattr(cls, "__init__", init_with_iam_stamp)


def _wrap_client_connect(cls: type, method_name: str) -> None:
    """包装 connect 路径：建连期间经 contextvar 暴露描述符给 SDK 工厂包装层。"""
    if (cls, method_name) in _wrapped_targets:
        return
    if not hasattr(cls, method_name):
        return
    _wrapped_targets.add((cls, method_name))
    orig_method = getattr(cls, method_name)

    async def connect_with_iam(self: Any, *args: Any, orig=orig_method, **kwargs: Any) -> Any:
        descriptor = getattr(self, "_jws_iam_descriptor", None)
        token = _pending_iam_descriptor.set(descriptor) if descriptor is not None else None
        try:
            return await orig(self, *args, **kwargs)
        finally:
            if token is not None:
                _pending_iam_descriptor.reset(token)

    setattr(cls, method_name, connect_with_iam)


def _wrap_sdk_factory(module: object, factory_name: str) -> None:
    """包装 mcp SDK 工厂函数：描述符在场时把 ``auth=`` 换成签名 Provider。

    原 ``auth``（静态 AuthHeaderAndQueryProvider，用户额外头/查询参数）作为
    inner 保留——先套静态头再算签名，inner 合入的 query 参数参与签名。
    """
    if (module, factory_name) in _wrapped_targets:
        return
    _wrapped_targets.add((module, factory_name))
    orig_factory = getattr(module, factory_name)

    def factory_with_iam(*args: Any, orig=orig_factory, **kwargs: Any) -> Any:
        descriptor = _pending_iam_descriptor.get()
        if descriptor is not None:
            kwargs["auth"] = HuaweiCloudIamAuthProvider(
                descriptor,
                inner=kwargs.get("auth"),
            )
            logger.info(
                "[mcp-iam-signing] per-request %s signing enabled (algorithm=%s)",
                factory_name,
                descriptor.get("algorithm"),
            )
        return orig(*args, **kwargs)

    setattr(module, factory_name, factory_with_iam)


def apply_mcp_iam_signing_patch() -> None:
    """应用 IAM 签名布线补丁。进程内幂等（``_PATCHED`` 哨兵）。

    在 ``JiuWenSwarmDeepAdapter.__init__`` 里与 ``apply_mcp_call_timeout_patch``
    一起调用；两个补丁都包装 ``SseClient._do_connect`` / SDK 工厂，包装即转发，
    顺序无关、可叠加。
    """
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    import mcp.client.sse as mcp_sse_module
    import mcp.client.streamable_http as mcp_streamable_http_module
    from openjiuwen.core.foundation.tool.mcp.client.sse_client import SseClient
    from openjiuwen.core.foundation.tool.mcp.client.streamable_http_client import (
        StreamableHttpClient,
    )

    _wrap_client_init(SseClient)
    _wrap_client_init(StreamableHttpClient)

    # 真实 SseClient 在 owner-task 的 _do_connect 里建 transport；单测假类可能
    # 只有 connect（与 mcp_call_timeout_patch 的取径口径一致）。
    if hasattr(SseClient, "_do_connect"):
        _wrap_client_connect(SseClient, "_do_connect")
    else:
        _wrap_client_connect(SseClient, "connect")
    _wrap_client_connect(StreamableHttpClient, "connect")

    _wrap_sdk_factory(mcp_sse_module, "sse_client")
    _wrap_sdk_factory(mcp_streamable_http_module, "streamablehttp_client")

    logger.info(
        "[mcp-iam-signing] patch applied (covered=SseClient,StreamableHttpClient,"
        "sse_client,streamablehttp_client)"
    )


__all__ = [
    "ALGORITHM_V1",
    "ALGORITHM_V11",
    "IAM_AUTH_PARAMS_KEY",
    "HuaweiCloudIamAuthProvider",
    "IamSigningDomainNotAllowedError",
    "allowed_iam_signing_domains",
    "apply_mcp_iam_signing_patch",
    "parse_huawei_cloud_iam_auth",
]
