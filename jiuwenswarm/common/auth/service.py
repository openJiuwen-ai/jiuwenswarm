# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""登录服务：各 channel 共用的唯一入口。

把「流程」（:class:`~jiuwenswarm.common.auth.account_kit.AccountKitFlow`）和
「会话」（:class:`~jiuwenswarm.common.auth.session_store.AuthSessionStore`）粘起来：

* 发起授权 / 浏览器回调 / 发起方认领 / 登出 —— 接在 HTTP 路由上
  （``jiuwenswarm/gateway/channel_manager/web/web_http_auth.py``）
* :func:`live_session` / :func:`resolve_id_token` —— 给模型层取凭据
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from jiuwenswarm.common.auth.account_kit import (
    CLAIM_POLL_INTERVAL_S,
    STATE_TTL_S,
    AccountKitFlow,
    OAuthError,
    login_enabled,
    mask,
)
from jiuwenswarm.common.auth.session_store import (
    AuthSession,
    AuthSessionStore,
    get_session_store,
)

logger = logging.getLogger(__name__)

#: id_token 剩余有效期低于它就续期。id_token 只有 1 小时，而 Gateway 的转发热路径
#: 不能同步续（会卡事件循环），所以得趁它还能用的时候在后台先换好。
REFRESH_AHEAD_S = 5 * 60.0


class AuthService:
    def __init__(
        self,
        flow: AccountKitFlow | None = None,
        store: AuthSessionStore | None = None,
    ) -> None:
        self._flow = flow or AccountKitFlow()
        self._store = store or get_session_store()
        # 按 user_id 的续期锁：同一账号的并发续期合并成一次
        self._refresh_locks: dict[str, threading.Lock] = {}
        self._refresh_locks_guard = threading.Lock()
        self._background_refreshing: set[str] = set()

    @property
    def flow(self) -> AccountKitFlow:
        return self._flow

    @property
    def store(self) -> AuthSessionStore:
        return self._store

    @property
    def enabled(self) -> bool:
        return login_enabled()

    def create_authorization_request(self) -> dict[str, Any]:
        """返回 ``{authorizeUrl, state, claimToken, expiresIn}``，调用方负责打开浏览器。

        回调落 ECS 时（配置里给了 ``callback_url``），顺带起一个后台线程去 ECS 轮询认领：
        浏览器的回调不会再回到本进程，授权码只能主动去取。前端那边完全不用变——它照旧向
        本地 ``/auth/claim`` 认领，在这个线程把会话建好之前一直拿到 202。
        """
        request = self._flow.create_authorization_request()
        if self._flow.config.callback_url:
            self._poll_server_callback_in_background(request["state"], request["claimToken"])
        return request

    def _poll_server_callback_in_background(self, state: str, claim_token: str) -> None:
        def _run() -> None:
            deadline = time.time() + STATE_TTL_S
            while time.time() < deadline:
                time.sleep(CLAIM_POLL_INTERVAL_S)
                if self._flow.pending.get(state) is None:
                    return  # 已经完成、被取消或已过期，没人在等这个结果了
                try:
                    claimed = self._flow.claim_from_exchange(state, claim_token)
                except Exception:  # noqa: BLE001 — 轮询线程不能把异常抛到无人接管的地方
                    logger.warning("[Auth] 认领授权码异常，稍后重试", exc_info=True)
                    continue
                if claimed is None:
                    continue
                code, error = claimed
                try:
                    self.complete_callback(state, code=code, error_obj=error)
                except OAuthError:
                    pass  # 失败已经登记给发起方了（complete_callback 内部做的）
                return
            logger.info("[Auth] 等待授权超时 state=%s", mask(state))

        threading.Thread(target=_run, name="auth-claim-poll", daemon=True).start()

    def complete_callback(
        self,
        state: str,
        code: str = "",
        error: str = "",
        error_description: str = "",
        error_obj: OAuthError | None = None,
    ) -> AuthSession:
        """回调结果到手：换 token → 建会话 → 登记结果等发起方来认领。

        失败也要登记：发起方在等这个 state 的结果，不告诉它就只能干等到过期。
        state 本身无效的情况例外——那不是任何人发起的登录，没有人在等。
        """
        pending = self._flow.pending.begin_callback(state)
        try:
            if error_obj is not None:
                raise error_obj
            if error:
                if error == "access_denied":
                    raise OAuthError("已取消授权，请重新发起登录", "oauth_access_denied")
                detail = f"（{error_description}）" if error_description else ""
                raise OAuthError(f"华为账号授权失败：{error}{detail}", "oauth_callback_failed")
            if not code:
                raise OAuthError("回调里没有授权码", "oauth_callback_missing_params")
            outcome = self._flow.exchange_code(code, pending.code_verifier)
        except OAuthError as err:
            self._flow.pending.finish(state, error=err)
            raise

        try:
            session = self._store.create(outcome)
        except Exception as exc:  # noqa: BLE001 — 存档写失败也得告诉发起方，否则它一直等到过期
            err = OAuthError("登录会话保存失败，请重试", "session_store_failed")
            self._flow.pending.finish(state, error=err)
            logger.warning("[Auth] 登录会话保存失败: %s", exc, exc_info=True)
            raise err from exc
        self._flow.pending.finish(state, session_id=session.session_id)
        # 模型目录放到后台拉，别让登录陪着等 APIG（不通时最长 10 秒）。发起方认领成功后
        # 重拉模型列表时，目录还没好的话 models.list 会自己再发现一次。
        threading.Thread(
            target=self.refresh_model_catalog,
            args=(session.session_id,),
            name="auth-catalog-refresh",
            daemon=True,
        ).start()
        return session

    def claim(self, state: str, claim_token: str) -> AuthSession | None:
        """发起方取回登录结果。回调还没到返回 ``None``；登录失败抛 :class:`OAuthError`。"""
        session_id = self._flow.pending.claim(state, claim_token)
        if session_id is None:
            return None
        session = self._store.get(session_id)
        if session is None:
            # 回调和认领之间被登出了（或同账号在别处又登了一次，把它顶掉了）
            raise OAuthError("登录会话已失效，请重新登录", "session_expired")
        return session

    def logout(self, session_id: str | None) -> None:
        """登出**这个**会话。没有会话 id 就什么都不做。

        不能把"没带会话 id"当成"全部登出"：cookie 丢了的浏览器点一下退出，就会把这台
        机器上别的浏览器正在用的登录全清掉。
        """
        if not session_id:
            return
        self._store.remove(session_id)
        # 清掉模型目录，避免下一个用户看到上一个用户的模型
        from jiuwenswarm.common.auth.model_catalog import clear_cache

        clear_cache()

    @staticmethod
    def refresh_model_catalog(session_id: str | None = None) -> int:
        from jiuwenswarm.common.auth.model_catalog import refresh

        try:
            return len(refresh(session_id))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Auth] 刷新模型目录失败: %s", exc)
            return 0

    def resolve_session(self, session_id: str | None = None) -> AuthSession | None:
        """**严格**按 session id 取会话；没有 id 就是没有会话。

        这里绝不能回落到「当前唯一登录用户」——那等于一个不带 cookie 的请求也能
        拿到别人的登录态，实际上取消了会话校验。
        """
        if not session_id:
            return None
        return self._store.get(session_id)

    def status(self, session_id: str | None = None) -> dict[str, Any]:
        base: dict[str, Any] = {"enabled": self.enabled, "provider": "huawei-account"}
        session = self.resolve_session(session_id)
        if session is None:
            return {**base, "islogin": False, "userId": None}
        session = self.ensure_fresh(session)
        if session is None:
            return {**base, "islogin": False, "userId": None, "expired": True}
        return {**base, "islogin": True, **session.public_view()}

    def ensure_fresh(self, session: AuthSession, *, blocking: bool = True) -> AuthSession | None:
        """返回凭据可用的会话；过期且续不了返回 ``None``。

        快过期（:data:`REFRESH_AHEAD_S` 内）就续。``blocking=False`` 给不能等网络的
        调用方（Gateway 转发热路径、逐请求的模型解析）：续期丢到后台线程，本次先用
        还没过期的旧 token；已经过期的这次就拿不到，下一次请求就能用上后台换好的。
        """
        if not session.credential.expires_within(REFRESH_AHEAD_S):
            return session
        if blocking:
            return self.try_refresh(session)
        # 先判再起线程：后台续期会原地替换 session.credential
        expired = session.credential.is_expired()
        self._refresh_in_background(session)
        return None if expired else session

    def _refresh_lock_for(self, user_id: str) -> threading.Lock:
        with self._refresh_locks_guard:
            lock = self._refresh_locks.get(user_id)
            if lock is None:
                lock = threading.Lock()
                self._refresh_locks[user_id] = lock
            return lock

    def try_refresh(self, session: AuthSession, *, ahead_s: float = REFRESH_AHEAD_S) -> AuthSession | None:
        """续期并返回最新的会话。同一账号的并发续期合并成一次。

        拿到锁后**重新读一次会话**做双重检查：排队期间往往已经有人刷好了，重读还会按 mtime
        同步磁盘，另一个进程（AgentServer）刚刷过也能发现。

        续期失败时旧 id_token 没过期就照常返回。``ahead_s``：离过期不到这么久才真的去续。
        """
        with self._refresh_lock_for(session.user_id):
            current = self._store.get(session.session_id)
            if current is None:
                logger.info("[Auth] 会话已不存在，放弃续期 user=%s", mask(session.user_id))
                return None
            if not current.credential.expires_within(ahead_s):
                return current

            refreshed = self._flow.refresh(current.credential)
            if refreshed is None:
                if current.credential.is_expired():
                    logger.info("[Auth] 凭据已过期且无法续期，需要重新登录 user=%s", mask(current.user_id))
                    return None
                return current
            updated = self._store.update_credential(current.session_id, refreshed)
            if updated is None:
                # 续期期间被登出了：新凭据不写回，也不当作续期成功
                logger.info("[Auth] 续期完成时会话已注销，丢弃结果 user=%s", mask(current.user_id))
                return None
            logger.info("[Auth] 凭据已续期 user=%s", mask(current.user_id))
            return updated

    def _refresh_in_background(self, session: AuthSession) -> None:
        with self._refresh_locks_guard:
            if session.user_id in self._background_refreshing:
                return
            self._background_refreshing.add(session.user_id)

        def _run() -> None:
            try:
                self.try_refresh(session)
            except Exception:  # noqa: BLE001 — 后台续期失败只记日志，下次再试
                logger.warning("[Auth] 后台续期异常 user=%s", mask(session.user_id), exc_info=True)
            finally:
                with self._refresh_locks_guard:
                    self._background_refreshing.discard(session.user_id)

        threading.Thread(target=_run, name="auth-refresh", daemon=True).start()


_service: AuthService | None = None
_service_lock = threading.Lock()


def get_auth_service() -> AuthService:
    """进程级单例。"""
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = AuthService()
    return _service


def reset_auth_service_for_test(service: AuthService | None = None) -> None:
    global _service
    with _service_lock:
        _service = service


class ModelAuthRequired(Exception):

    def __init__(self, message: str, reason: str = "not_logged_in") -> None:
        super().__init__(message)
        self.reason = reason


def live_session(session_id: str | None, allow_refresh: bool = True) -> AuthSession:
    """取**这个会话**当前可用的登录态，拿不到抛 :class:`ModelAuthRequired`。

    **没有会话 id 就是没登录**，绝不退回「本机唯一登录会话」。早先有过这个兜底，
    结果是无痕窗口、没带 cookie 的请求、别的机器连上来的客户端，统统用上了本机
    登录用户的 id_token，额度也记到他头上。凭据只能沿着请求带下来的会话 id 取。

    ``allow_refresh=False`` 给**不能阻塞的调用方**：续期是一次同步 HTTP，挂在事件
    循环上会拖住所有连接。这时续期改在后台做（见 :meth:`AuthService.ensure_fresh`）。
    """
    service = get_auth_service()
    session = service.resolve_session(session_id)
    if session is None:
        raise ModelAuthRequired("请先登录后再使用该模型", "not_logged_in")
    fresh = service.ensure_fresh(session, blocking=allow_refresh)
    if fresh is None:
        raise ModelAuthRequired("登录已过期，请重新登录", "session_expired")
    return fresh


def resolve_id_token(session_id: str | None, allow_refresh: bool = True) -> str:
    """调 APIG 用的 id_token（``Authorization: Bearer <id_token>``）。"""
    return live_session(session_id, allow_refresh).credential.id_token
