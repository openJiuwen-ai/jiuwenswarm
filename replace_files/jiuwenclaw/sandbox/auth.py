# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
import hashlib
import json
import time
import uuid
from typing import Dict, Union
from cryptography.hazmat.primitives import serialization
from jwt import PyJWT
import logging

logger = logging.getLogger(__name__)
AUTHORIZATION_PREFIX = 'Bearer '


# private key
class PrivateKey(object):

    def __init__(self, client_id, kid, private_key):
        self.client_id = client_id
        self.kid = kid
        self.private_key = private_key


def generate_jwt(private_key: PrivateKey, signed_header: dict, request_json_str: str) -> str:
    from jiuwenclaw.sts_service import JWT_AUD_URI
    try:
        algorithm = serialization.load_der_private_key(bytes.fromhex(private_key.private_key), None)
        iat = int(time.time())
        header_claims = {
            "typ": "JWT-PRO2",
            "kid": private_key.kid,
            "alg": 'RS256',
            "sdkVersion": 20200
        }
        lower_key_signed_headers = _to_lower_key(signed_header)
        exp = iat + 3000000
        request_signature = _get_request_signature(lower_key_signed_headers, request_json_str)
        payload = {
            "aud": JWT_AUD_URI,
            "iss": private_key.client_id,
            "exp": exp,
            "iat": iat,
            "signedHeaders": _get_signed_headers(lower_key_signed_headers),
            "requestSignature": request_signature
        }
        instance = PyJWT()
        token = instance.encode(
            payload=payload,
            key=algorithm,
            algorithm="RS256",
            headers=header_claims
        )

        return token

    except Exception as exception:
        raise RuntimeError(f"error while generating jwt: {str(exception)}") from exception


def _to_lower_key(signed_header: dict):
    result_dict = {}
    for k, v in signed_header.items():
        result_dict[str(k).lower()] = v
    return result_dict


def _get_signed_headers(signed_headers: dict):
    if signed_headers:
        keys = list(signed_headers.keys())
        keys.sort()
        return ";".join(keys)
    else:
        return ""


def _get_request_signature(signed_headers: dict, request_body):
    canonical_headers = ""
    canonical_headers_1 = ""
    if signed_headers:
        keys = list(signed_headers.keys())
        keys.sort()
        for header_key in keys:
            item_str = str(header_key).lower() + ":" + _trim_map_value(signed_headers.get(header_key)) + "\n"
            canonical_headers += item_str
    # 长度不为空时
    if not canonical_headers:
        canonical_headers += "\n"

    if request_body:
        sha_hex_str = hashlib.sha256(request_body.encode('utf-8')).hexdigest()
        canonical_headers_1 = "".join([canonical_headers, sha_hex_str])
    else:
        canonical_headers_1 = canonical_headers
    return hashlib.sha256(canonical_headers_1.encode('utf-8')).hexdigest()


def _trim_map_value(value):
    if value is None:
        return ""
    else:
        return str(value).strip()


def sha256(payload_bytes: bytes):
    # 创建一个sha256 hash对象
    sha256_hash = hashlib.sha256()

    # 更新hash对象，使用要哈希的数据
    sha256_hash.update(payload_bytes)  # 注意需要将字符串编码为字节串

    # 获取十六进制格式的哈希值
    payload_hex = sha256_hash.hexdigest()
    return payload_hex


def rebuild_payload(request_payload, http_request_path, http_method, http_query_string):
    if http_query_string != '':
        return request_payload + '\n' + http_request_path + '\n' + http_method + '\n' + http_query_string
    else:
        return request_payload + '\n' + http_request_path + '\n' + http_method


def get_sts_private_key():
    from jiuwenclaw.sts_service import sts_config
    # 获取加密的sts配置
    encrypt_config = sts_config.get_sts_config(sts_config.sensitive_config)
    decrypt_sensitive = json.loads(encrypt_config)
    if decrypt_sensitive is None:
        logger.error(f"decrypt_sensitive is null")

    key = PrivateKey(
        str(decrypt_sensitive.get('clientId')),
        decrypt_sensitive.get('keyId'),
        decrypt_sensitive.get('privateKey')
    )
    return key


def get_payload_hex(payload: Union[Dict, str]) -> str:
    # 判断 payload 的类型
    if isinstance(payload, dict):
        # 处理 json 类型的 payload
        # 转换 payload 为 string 类型, 注意服务测为java的toString()方法会去掉空格，python要handle这个case
        payload_str = json.dumps(payload, separators=(',', ':'), ensure_ascii=False)
        if payload_str == '{}':
            payload_str = ''
    elif isinstance(payload, str):
        # 处理字符串类型的 payload
        payload_str = payload
    else:
        raise RuntimeError(f"payload must be either a dict or a str")

    # 计算哈希的字符串
    payload_hex = sha256(payload_str.encode('utf-8'))

    return payload_hex


def get_jwt(signed_header: dict, payload_str: str, uri: str, method: str = 'POST', query: str = '') -> str:
    logger.info(f"开始生成 JWT")
    # 获取private_key
    private_key = get_sts_private_key()
    # 生成 request_json_str
    request_json_str = rebuild_payload(payload_str, uri, method, query)

    return generate_jwt(
        private_key=private_key,
        signed_header=signed_header,
        request_json_str=request_json_str
    )


def build_headers(payload: dict, uri: str, method: str, query: str, sidecar: bool = False) -> dict:
    from jiuwenclaw.sts_service import SERVICE_ACCOUNT_CLIENT_ID
    # 设置需要签名的请求头 x-trace-id , x-client-id
    trace_id = str(uuid.uuid4())
    logger.info(f"trace_id: {trace_id}")
    headers = {
        "x-trace-id": trace_id,
        "x-client-id": SERVICE_ACCOUNT_CLIENT_ID,
    }
    # 转换 payload
    payload_hex = get_payload_hex(payload)
    # 获取 jwt
    token = get_jwt(headers, payload_hex, uri, method, query)
    # 组装请求头
    headers.update({"Authorization": f"{AUTHORIZATION_PREFIX}{token}"})
    # 伴生沙箱上传文件需要增加请求头
    if sidecar:
        headers.update({"Sandbox-Target-Endpoint": "secguard"})
    return headers
