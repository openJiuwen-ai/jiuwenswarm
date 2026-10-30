"""请求级扩展字段透传（Web 握手 query / header → Message.metadata['ext']）。

将一个 ``dict[str, Any]`` 从 Channel 入口写入 ``Message.metadata[METADATA_KEY]``，
由 AgentServer 入口抬升到请求级 ContextVar，供 Agent rail 通过 :func:`get_ext` 读取。

要透传哪些字段由环境变量声明（逗号分隔），两套名字等价，先读 Swarm 名：

- ``JIUWENSWARM_REQUEST_EXT_FORWARD_HEADERS``
- ``JIUWENCLAW_REQUEST_EXT_FORWARD_HEADERS``（企业 kub 兼容）

运行时可调 :func:`set_forward_headers` 覆盖。载体内的键名与值含义由集成方约定。
"""
from __future__ import annotations

import base64
import json
import os
from contextvars import ContextVar, Token
from typing import Any, Mapping

ENV_FORWARD_HEADERS = "JIUWENSWARM_REQUEST_EXT_FORWARD_HEADERS"
ENV_FORWARD_HEADERS_LEGACY = "JIUWENCLAW_REQUEST_EXT_FORWARD_HEADERS"
METADATA_KEY = "ext"

# Gateway -> AgentServer 的内部 REST 载体。浏览器侧仍使用配置白名单中的原始
# header/query，不能直接把本头当作公开入口或鉴权凭据。
INTERNAL_HEADER_NAME = "X-Jiuwenswarm-Request-Ext"

# 扩展字段只承载少量请求级键值。限制 JSON 为 4 KiB 后，Base64URL 值最大约
# 5.34 KiB，可为常见的单行 HTTP header 限制预留名称及其他头部空间。
MAX_INTERNAL_JSON_BYTES = 4 * 1024
MAX_INTERNAL_HEADER_CHARS = 6 * 1024


class RequestExtCodecError(ValueError):
    """内部请求扩展字段无法安全编码或解码。"""


_request_ext: ContextVar["dict[str, Any] | None"] = ContextVar(
    "jw_request_ext", default=None,
)

# 运行时覆盖。优先级：set_forward_headers > 环境变量 > 空（关闭）。
_runtime_override: "list[str] | None" = None


def set_forward_headers(headers: "list[str] | None") -> None:
    """整体覆盖 forward_headers（管控面 / DB 监听器使用）。

    传 None 等价于撤销覆盖，回落到环境变量；传空 list 等价于关闭透传。
    """
    global _runtime_override
    if headers is None:
        _runtime_override = None
    else:
        _runtime_override = [str(x).strip() for x in headers if str(x).strip()]


def register_forward_header(name: str) -> None:
    """加法注册单个字段名（扩展加载时使用）。"""
    global _runtime_override
    name = str(name).strip()
    if not name:
        return
    if _runtime_override is None:
        _runtime_override = list(_env_forward_headers())
    if name not in _runtime_override:
        _runtime_override.append(name)


def register_forward_headers(names: "list[str]") -> None:
    """加法注册多个字段名。"""
    for n in names or []:
        register_forward_header(n)


def _env_forward_headers() -> "list[str]":
    raw = os.environ.get(ENV_FORWARD_HEADERS, "") or os.environ.get(ENV_FORWARD_HEADERS_LEGACY, "")
    if not raw:
        return []
    return [x.strip() for x in raw.split(",") if x.strip()]


def _read_forward_headers() -> "list[str]":
    if _runtime_override is not None:
        return list(_runtime_override)
    return _env_forward_headers()


def build_ext_from_source(source: "Mapping[str, Any] | None") -> "dict[str, Any] | None":
    """从入站 mapping（HTTP header 或 WS query 字典）按配置抽取扩展字段。

    支持值为字符串或字符串列表（``parse_qs`` 输出）；列表取首元素。
    键名匹配大小写不敏感。
    """
    names = _read_forward_headers()
    if not names or not source:
        return None
    lower_to_actual = {k.lower(): k for k in source.keys() if isinstance(k, str)}
    out: dict[str, Any] = {}
    for name in names:
        # 内部载体只能由 Gateway 根据普通白名单字段重新生成，不能把外部同名头
        # 当作扩展值再次封装，更不能形成“客户端自称内部调用”的歧义。
        if name.lower() == INTERNAL_HEADER_NAME.lower():
            continue
        actual = lower_to_actual.get(name.lower())
        if actual is None:
            continue
        val = source[actual]
        if isinstance(val, (list, tuple)):
            val = val[0] if val else None
        if val is None or val == "":
            continue
        out[name] = val
    return out or None


def encode_internal_header(ext: "Mapping[str, Any] | None") -> "str | None":
    """将白名单过滤后的 ``ext`` 编码成内部 HTTP header 值。

    Base64URL 仅解决 Unicode、引号和换行等字符的 HTTP 传输安全问题，不提供
    加密、签名或鉴权能力。
    """
    if not ext:
        return None
    if not isinstance(ext, Mapping):
        raise RequestExtCodecError("request ext must be a mapping")
    try:
        raw = json.dumps(
            dict(ext),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RequestExtCodecError("request ext must contain JSON values") from exc
    if len(raw) > MAX_INTERNAL_JSON_BYTES:
        raise RequestExtCodecError(
            f"request ext JSON exceeds {MAX_INTERNAL_JSON_BYTES} bytes"
        )
    encoded = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    if len(encoded) > MAX_INTERNAL_HEADER_CHARS:
        raise RequestExtCodecError(
            f"encoded request ext exceeds {MAX_INTERNAL_HEADER_CHARS} characters"
        )
    return encoded


def decode_internal_header(value: "str | None") -> "dict[str, Any] | None":
    """解码 Gateway 生成的内部 HTTP header，并校验其大小和 JSON 类型。"""
    if value is None or not str(value).strip():
        return None
    encoded = str(value).strip()
    if len(encoded) > MAX_INTERNAL_HEADER_CHARS:
        raise RequestExtCodecError(
            f"encoded request ext exceeds {MAX_INTERNAL_HEADER_CHARS} characters"
        )
    if not all(ch.isascii() and (ch.isalnum() or ch in "-_") for ch in encoded):
        raise RequestExtCodecError("request ext is not valid Base64URL")
    try:
        encoded_bytes = encoded.encode("ascii")
        padding = b"=" * (-len(encoded_bytes) % 4)
        raw = base64.b64decode(
            encoded_bytes + padding,
            altchars=b"-_",
            validate=True,
        )
    except ValueError as exc:
        raise RequestExtCodecError("request ext is not valid Base64URL") from exc
    if len(raw) > MAX_INTERNAL_JSON_BYTES:
        raise RequestExtCodecError(
            f"request ext JSON exceeds {MAX_INTERNAL_JSON_BYTES} bytes"
        )
    try:
        decoded = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequestExtCodecError("request ext is not valid UTF-8 JSON") from exc
    if not isinstance(decoded, dict):
        raise RequestExtCodecError("request ext JSON must be an object")
    if not all(isinstance(key, str) for key in decoded):
        raise RequestExtCodecError("request ext keys must be strings")
    return decoded or None


def set_current(ext: "dict[str, Any] | None") -> "Token":
    """Channel 入口使用：将 ext 写入 ContextVar，返回还原 token。"""
    return _request_ext.set(dict(ext) if ext else None)


def attach_to_metadata(
    metadata: "dict[str, Any] | None",
    ext: "dict[str, Any] | None" = None,
) -> "dict[str, Any] | None":
    """构造 Message 时使用：将 ext 写入 metadata。

    ``ext`` 缺省时读取当前 ContextVar 中的 ext。无 ext 时返回原 metadata。
    """
    if ext is None:
        ext = _request_ext.get()
    if not ext:
        return metadata
    out = dict(metadata or {})
    out[METADATA_KEY] = dict(ext)
    return out


def lift_from_metadata(metadata: "Mapping[str, Any] | None") -> "Token | None":
    """AgentServer 入口使用：从 request.metadata 抬升 ext 到 ContextVar。"""
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get(METADATA_KEY)
    if not isinstance(raw, dict) or not raw:
        return None
    return _request_ext.set(dict(raw))


def reset_ext(token: "Token | None") -> None:
    """与 :func:`set_current` / :func:`lift_from_metadata` 配对，还原 ContextVar。"""
    if token is not None:
        _request_ext.reset(token)


def get_ext() -> "dict[str, Any]":
    """Agent rail 使用：读取当前请求的扩展字段。无字段时返回空 dict。"""
    cur = _request_ext.get()
    return dict(cur) if cur else {}
