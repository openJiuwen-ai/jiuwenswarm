# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""华为账号Account Kit登录，使用OAuth2授权码+PKCE"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

from jiuwenswarm.common.auth.net import requests_request
from jiuwenswarm.common.auth.remote_config import RemoteConfig
from jiuwenswarm.common.auth.remote_config import get_config as get_remote_config

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://oauth-login.cloud.huawei.com/oauth2/v3/authorize"

#: 本实现在官网配置接口里的段名
LOGIN_SECTION = "huaweiaccount_login"

#: AGC里的``oauth_client.client_id``
DEFAULT_CLIENT_ID = "118944053"
#: 回调落**本机 Gateway** 时的地址。必须和AGC里登记的逐字一致
DEFAULT_REDIRECT_URI = "http://localhost:19000/api/v1/auth/callback"
_CLAIM_FINGERPRINT_SALT = "jiuwen-account-kit-claim:"
_CLAIM_FINGERPRINT_LEN = 32
#: 回调落 ECS 时，发起方每隔这么久去认领一次，直到拿到结果或 state 过期
CLAIM_POLL_INTERVAL_S = 2.0
DEFAULT_SCOPE = "openid profile"
STATE_TTL_S = 10 * 60.0
CLAIM_TTL_S = 5 * 60.0
REQUEST_TIMEOUT_S = 15.0
_FALLBACK_TOKEN_TTL_S = 3600.0


class OAuthError(Exception):

    def __init__(self, message: str, code: str = "oauth_error") -> None:
        super().__init__(message)
        self.code = code


def mask(value: str | None) -> str:
    if not value:
        return ""
    return "***" if len(value) <= 8 else f"...{value[-8:]}"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_pkce() -> tuple[str, str]:
    """返回 ``(code_verifier, code_challenge)``，S256。"""
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def parse_id_token(id_token: str | None) -> dict[str, Any]:
    """解出 id_token 的 payload。**不验签。**"""
    if not id_token:
        return {}
    parts = id_token.split(".")
    if len(parts) < 2:
        return {}
    try:
        segment = parts[1]
        padded = segment + "=" * (-len(segment) % 4)
        parsed = json.loads(base64.urlsafe_b64decode(padded))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def login_enabled() -> bool:
    """登录功能是否开启。

    判据只有一条：官网配置接口给出了配置、且 ``is_effective`` 为真。配置地址有默认值，
    所以默认是开的；拉不到配置、或把 ``JIUWENSWARM_CONFIG_URL`` 显式设成 off，功能就是关的。
    """
    config = get_remote_config()
    return config is not None and config.is_effective


@dataclass(frozen=True)
class OAuthConfig:
    client_id: str
    redirect_uri: str
    scope: str
    exchange_url: str
    callback_url: str = ""
    claim_url: str = ""
    authorize_url: str = AUTHORIZE_URL

    @property
    def effective_redirect_uri(self) -> str:
        return self.callback_url or self.redirect_uri

    @property
    def effective_claim_url(self) -> str:
        if self.claim_url:
            return self.claim_url
        if not self.callback_url:
            return ""
        return urljoin(self.callback_url, "claim")

    @classmethod
    def from_remote(cls, config: "RemoteConfig | None" = None) -> "OAuthConfig":
        if config is None:
            config = get_remote_config()
        if config is None:
            return cls(client_id=DEFAULT_CLIENT_ID, redirect_uri=DEFAULT_REDIRECT_URI,
                       scope=DEFAULT_SCOPE, exchange_url="")
        login = config.login(LOGIN_SECTION)
        return cls(
            client_id=login.value("client_id", DEFAULT_CLIENT_ID),
            redirect_uri=login.value("redirect_uri", DEFAULT_REDIRECT_URI),
            scope=login.value("scope", DEFAULT_SCOPE),
            exchange_url=login.value("exchange_url"),
            callback_url=login.value("callback_url"),
            claim_url=login.value("claim_url"),
        )


@dataclass
class Credential:

    id_token: str = ""
    refresh_token: str = ""
    expires_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id_token": self.id_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Credential":
        data = data or {}
        try:
            expires_at = float(data.get("expires_at") or 0.0)
        except (TypeError, ValueError):
            expires_at = 0.0
        return cls(
            id_token=str(data.get("id_token") or ""),
            refresh_token=str(data.get("refresh_token") or ""),
            expires_at=expires_at,
        )

    def expires_within(self, seconds: float) -> bool:
        return not self.id_token or self.expires_at - seconds <= time.time()

    def is_expired(self, skew_s: float = 30.0) -> bool:
        return self.expires_within(skew_s)


@dataclass
class LoginOutcome:

    credential: Credential
    user_id: str
    user_name: str | None


def _outcome_from_token_response(payload: dict[str, Any], previous_refresh: str = "") -> LoginOutcome:
    id_token = str(payload.get("id_token") or "").strip()
    if not id_token:
        raise OAuthError("华为账号没有返回 id_token（scope 需包含 openid）", "oauth_token_exchange_failed")
    claims = parse_id_token(id_token)
    user_id = str(claims.get("openid") or claims.get("sub") or "").strip()
    if not user_id:
        raise OAuthError("id_token 里没有 openid", "oauth_token_exchange_failed")

    exp = claims.get("exp")
    if isinstance(exp, (int, float)) and not isinstance(exp, bool):
        expires_at = float(exp)
    else:
        expires_at = time.time() + _FALLBACK_TOKEN_TTL_S

    credential = Credential(
        id_token=id_token,
        refresh_token=str(payload.get("refresh_token") or "").strip() or previous_refresh,
        expires_at=expires_at,
    )
    user_name = str(claims.get("display_name") or claims.get("nickname") or "").strip() or None
    return LoginOutcome(credential=credential, user_id=user_id, user_name=user_name)


@dataclass
class PendingLogin:
    state: str
    code_verifier: str
    claim_token: str
    expires_at: float
    #: 回调已经到过，授权码是一次性的，同一个state的第二次回调不能再去换。
    callback_seen: bool = False
    session_id: str | None = None
    error: OAuthError | None = None


class PendingLogins:
    """按 state 索引的待完成登录，进程内存：回调和发起必须落到同一个进程"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, PendingLogin] = {}

    def add(self, code_verifier: str, *, fingerprint_state: bool = False) -> PendingLogin:
        claim_token = secrets.token_urlsafe(32)
        state = secrets.token_urlsafe(24)
        if fingerprint_state:
            state = f"{state}.{claim_fingerprint(claim_token)}"
        pending = PendingLogin(
            state=state,
            code_verifier=code_verifier,
            claim_token=claim_token,
            expires_at=time.time() + STATE_TTL_S,
        )
        with self._lock:
            self._prune()
            self._items[pending.state] = pending
        return pending

    def get(self, state: str) -> PendingLogin | None:
        with self._lock:
            self._prune()
            return self._items.get(state)

    def begin_callback(self, state: str) -> PendingLogin:
        with self._lock:
            self._prune()
            pending = self._items.get(state)
            if pending is None:
                raise OAuthError("登录请求无效或已过期，请重新发起登录", "oauth_state_invalid")
            if pending.callback_seen:
                raise OAuthError("该登录请求已处理过，请回到应用查看结果", "oauth_callback_replayed")
            pending.callback_seen = True
            return pending

    def finish(self, state: str, *, session_id: str | None = None, error: OAuthError | None = None) -> None:
        with self._lock:
            pending = self._items.get(state)
            if pending is None:
                return
            pending.session_id = session_id
            pending.error = error
            pending.expires_at = time.time() + CLAIM_TTL_S

    def claim(self, state: str, claim_token: str) -> str | None:
        with self._lock:
            self._prune()
            pending = self._items.get(state)
            if pending is None or not hmac.compare_digest(pending.claim_token, claim_token):
                raise OAuthError("登录请求无效或已过期，请重新发起登录", "oauth_state_invalid")
            if pending.error is not None:
                self._items.pop(state, None)
                raise pending.error
            if pending.session_id is None:
                return None
            self._items.pop(state, None)
            return pending.session_id

    def _prune(self) -> None:
        now = time.time()
        for state in [s for s, p in self._items.items() if p.expires_at <= now]:
            self._items.pop(state, None)


def claim_fingerprint(claim_token: str) -> str:
    """``claim_token`` → 放进 state 里的指纹。

    回调落 ECS 时，ECS 凭它确认"来认领的确实是发起这次登录的客户端"，因此发起方不用
    提前去登记，ECS 的多个实例之间也没有要同步的账本。取哈希而不是原值：state 会进地址栏、
    浏览器历史和华为的日志。**规则必须和ECS侧一字不差**
    """
    digest = hashlib.sha256((_CLAIM_FINGERPRINT_SALT + (claim_token or "")).encode("utf-8"))
    return digest.hexdigest()[:_CLAIM_FINGERPRINT_LEN]


def _is_plaintext_remote(url: str) -> bool:
    parsed = urlparse(url or "")
    return parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1")


class AccountKitFlow:
    def __init__(self, config: OAuthConfig | None = None) -> None:
        self._config_override = config
        self.pending = PendingLogins()
        self._warned_plaintext = False

    @property
    def config(self) -> OAuthConfig:
        return self._config_override or OAuthConfig.from_remote()

    def create_authorization_request(self) -> dict[str, Any]:
        """返回 ``{authorizeUrl, state, claimToken, expiresIn}``。"""
        self._ensure_exchange_available()
        config = self.config
        code_verifier, code_challenge = generate_pkce()
        pending = self.pending.add(code_verifier, fingerprint_state=bool(config.callback_url))
        params = {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": config.effective_redirect_uri,
            "scope": config.scope,
            "state": pending.state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            # 要refresh_token需要它
            "access_type": "offline",
        }
        logger.info(
            "[Auth] 已生成授权地址 state=%s 回调=%s",
            mask(pending.state),
            "ECS" if config.callback_url else "本机",
        )
        return {
            "authorizeUrl": f"{config.authorize_url}?{urlencode(params)}",
            "state": pending.state,
            "claimToken": pending.claim_token,
            "expiresIn": int(STATE_TTL_S),
        }

    def exchange_code(self, code: str, code_verifier: str) -> LoginOutcome:
        payload = self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": code_verifier,
                # 必须和授权时发给华为的那个一字不差，否则华为拒绝
                "redirect_uri": self.config.effective_redirect_uri,
            }
        )
        outcome = _outcome_from_token_response(payload)
        logger.info("[Auth] 授权码已换到 token user=%s", mask(outcome.user_id))
        return outcome

    def claim_from_exchange(self, state: str, claim_token: str) -> tuple[str, OAuthError | None] | None:
        """回调落 ECS 时：去 ECS 取这次登录的授权码。

        返回 ``(code, None)``、``("", OAuthError)``（用户取消等），回调还没到返回 ``None``。
        网络抖动也返回 ``None``——由调用方的轮询下次再试，不该把一次登录判死。
        """
        claim_url = self.config.effective_claim_url
        if not claim_url:
            return None
        try:
            response = requests_request(
                "POST",
                claim_url,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={"state": state, "claim_token": claim_token},
                timeout=REQUEST_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 — 轮询期间的网络抖动不算失败
            logger.warning("[Auth] 认领授权码失败（稍后重试）: %s", exc)
            return None
        if response.status_code == 202:
            return None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if not isinstance(payload, dict):
            logger.warning("[Auth] 认领接口返回了非 JSON（HTTP %s）", response.status_code)
            return None
        if response.status_code == 200 and payload.get("code"):
            return str(payload["code"]), None
        error = str(payload.get("error") or "")
        if error == "access_denied":
            return "", OAuthError("已取消授权，请重新发起登录", "oauth_access_denied")
        if error:
            detail = str(payload.get("error_description") or "")
            return "", OAuthError(
                f"华为账号授权失败：{error}{f'（{detail}）' if detail else ''}", "oauth_callback_failed"
            )
        if response.status_code != 200:
            logger.warning("[Auth] 认领被拒绝 HTTP %s", response.status_code)
            return "", OAuthError("登录请求无效或已过期，请重新发起登录", "oauth_state_invalid")
        return None

    def refresh(self, credential: Credential) -> Credential | None:
        """用 refresh_token 续期。拿不到新凭据返回 ``None``，由调用方引导重新登录。"""
        if not credential.refresh_token:
            return None
        try:
            payload = self._token_request(
                {"grant_type": "refresh_token", "refresh_token": credential.refresh_token}
            )
            return _outcome_from_token_response(payload, credential.refresh_token).credential
        except OAuthError as error:
            logger.warning("[Auth] 续期失败 (%s): %s", error.code, error)
            return None

    def _ensure_exchange_available(self) -> None:
        if _is_plaintext_remote(self.config.exchange_url) and not self._warned_plaintext:
            self._warned_plaintext = True
            logger.warning(
                "[Auth] token 交换服务走的是明文 HTTP（%s）：响应里有 refresh_token，只能用于联调",
                self.config.exchange_url,
            )
        if not self.config.exchange_url:
            raise OAuthError(
                "未配置 token 交换服务（配置接口没下发 login.exchange_url）",
                "exchange_not_configured",
            )

    def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        self._ensure_exchange_available()
        # ECS补上client_secret再转给登录页面，client_id也带上让它核对
        data = {**form, "client_id": self.config.client_id}
        try:
            response = requests_request(
                "POST",
                self.config.exchange_url,
                # 必须是form编码，不是 JSON
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data=data,
                timeout=REQUEST_TIMEOUT_S,
            )
        except Exception as exc:  # noqa: BLE001 — 网络层异常统一转业务错误
            logger.warning("[Auth] token 交换服务不可达: %s", exc)
            raise OAuthError("无法连接登录服务，请检查网络后重试", "oauth_network_error") from exc

        try:
            payload = response.json()
        except ValueError:
            payload = None
        if response.status_code != 200 or not isinstance(payload, dict):
            error = payload.get("error") if isinstance(payload, dict) else None
            logger.warning(
                "[Auth] token 请求失败 grant=%s status=%s error=%s sub_error=%s",
                form.get("grant_type"),
                response.status_code,
                error,
                payload.get("sub_error") if isinstance(payload, dict) else None,
            )
            raise OAuthError(
                f"换取登录凭据失败（HTTP {response.status_code}{f'，{error}' if error else ''}）",
                "oauth_token_exchange_failed",
            )
        return payload
