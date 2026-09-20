# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""登录会话存储：内存 + 加密落盘。

存档里有 refresh_token——它是长期凭据，明文落盘等于把账号的模型额度交出去，所以
一律 AES-256-GCM 加密，密钥放在 ``~/.jiuwenswarm/auth/.install_key``（0600），
每台机器一把。

落盘是为了**进程重启后不用重新登录**（id_token 只有 1 小时，靠 refresh_token 续期），
也是 Gateway 与 AgentServer 两个进程共享登录态的途径。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from jiuwenswarm.common.auth.account_kit import Credential, LoginOutcome, mask
from jiuwenswarm.common.utils import get_user_workspace_dir

logger = logging.getLogger(__name__)

_KEY_FILE_NAME = ".install_key"
#: 密文信封的版本与算法。换算法时版本号 +1，旧数据靠它识别。
_ENVELOPE_VERSION = 1
_ENVELOPE_ALG = "A256GCM"
_SESSION_FILE_NAME = "sessions.json"
#: 会话存档的格式版本。认不出的版本一律当作没登录，不做迁移（重新登录一次即可）。
_ARCHIVE_VERSION = 2
# 会话最长存活时间。id_token 只有 1h，会话更长是为了让「续期」有机会跑起来。
DEFAULT_SESSION_TTL_S = 30 * 24 * 60 * 60.0


def auth_dir() -> Path:
    path = Path(get_user_workspace_dir()) / "auth"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _restrict(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover — 平台差异
        logger.debug("[Auth] chmod 0600 失败（平台不支持）: %s", path)


def _key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:8]


def _parse_install_key(text: str) -> bytes | None:
    try:
        raw = base64.urlsafe_b64decode(text.strip())
    except ValueError:
        return None
    return raw if len(raw) == 32 else None


def _publish_new_file(tmp: Path, target: Path) -> bool:
    try:
        if os.name == "nt":
            os.rename(tmp, target)  # Windows 上目标已存在就失败，不会覆盖
        else:
            os.link(tmp, target)  # 硬链接同样不覆盖已有文件
            tmp.unlink()
    except FileExistsError:
        tmp.unlink(missing_ok=True)
        return False
    return True


def _install_key(create: bool) -> bytes | None:
    """本机密钥文件。``create=False`` 时只读不建（没有或已损坏返回 ``None``）。

    **读不了就抛出去，绝不当成"没有密钥"去重建**：文件被杀毒软件、备份工具短暂占用时重建
    会覆盖掉好密钥，之前保存的会话从此永远解不开。只有内容确实损坏才重建，旧文件改名留档。

    **新建是原子的、不覆盖的**：两个进程首次启动可能同时走到这里，撞上时用对方放好的那把。
    """
    key_path = auth_dir() / _KEY_FILE_NAME
    if key_path.exists():
        existing = _parse_install_key(key_path.read_text(encoding="utf-8"))
        if existing is not None:
            return existing
        if not create:
            return None
        backup = key_path.with_name(f"{_KEY_FILE_NAME}.corrupt-{int(time.time())}")
        key_path.replace(backup)
        logger.error(
            "[Auth] 本机密钥文件已损坏，已改名为 %s 并重新生成；之前保存的登录会话无法解密，需要重新登录",
            backup.name,
        )
    elif not create:
        return None

    key = secrets.token_bytes(32)
    tmp = key_path.with_name(f"{_KEY_FILE_NAME}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(base64.urlsafe_b64encode(key).decode("ascii"), encoding="utf-8")
    _restrict(tmp)
    if _publish_new_file(tmp, key_path):
        return key
    existing = _parse_install_key(key_path.read_text(encoding="utf-8"))
    if existing is None:
        raise ValueError("另一个进程刚写入的本机密钥文件内容无效")
    return existing


def _keyring() -> tuple[bytes, dict[str, bytes]]:
    """返回 ``(用于加密的密钥, 所有可解密的密钥 by kid)``。

    密钥就是本机的 ``.install_key``。会话是一台机器一份，没有跨机器共享的形态；
    真要做多副本共享登录态时，存档本身也得挪到共享存储，到时一起加。
    """
    primary = _install_key(create=True)
    if primary is None:
        raise ValueError("本机密钥不可用")
    return primary, {_key_id(primary): primary}


def derive_local_secret(label: bytes) -> bytes:
    """从加密会话用的主密钥派生一把专用密钥：``sha256(label + 主密钥)``。

    给需要"本机 / 本集群稳定、外人算不出"的地方用（比如凭据句柄的 HMAC），
    不把加密密钥本身交出去。``label`` 区分用途，不同用途派生出的密钥互不相同。
    """
    primary, _ = _keyring()
    return hashlib.sha256(label + primary).digest()


def encrypt_json(value: Any) -> str:
    """AES-256-GCM，输出带版本 / 算法 / 密钥 id 的信封。

    字段名对齐 relay-claw 的 ``encryptJson``（``huawei-cas.ts``），便于两边互读。
    有版本位才换得了算法，也才分得清"格式不对"和"密钥不对"。
    """
    primary, _ = _keyring()
    nonce = secrets.token_bytes(12)
    plaintext = json.dumps(value, ensure_ascii=False).encode("utf-8")
    sealed = AESGCM(primary).encrypt(nonce, plaintext, None)
    # AESGCM 把 tag 拼在密文尾部；拆开存是为了和 relay-claw 的信封结构一致
    ciphertext, tag = sealed[:-16], sealed[-16:]
    return json.dumps(
        {
            "version": _ENVELOPE_VERSION,
            "alg": _ENVELOPE_ALG,
            "kid": _key_id(primary),
            "iv": base64.urlsafe_b64encode(nonce).decode("ascii"),
            "tag": base64.urlsafe_b64encode(tag).decode("ascii"),
            "data": base64.urlsafe_b64encode(ciphertext).decode("ascii"),
        },
        separators=(",", ":"),
    )


def decrypt_json(raw: str) -> Any:
    _, keys = _keyring()
    envelope = json.loads((raw or "").strip())
    if not isinstance(envelope, dict):
        raise ValueError("密文不是信封格式")
    if envelope.get("version") != _ENVELOPE_VERSION or envelope.get("alg") != _ENVELOPE_ALG:
        raise ValueError(
            f"不支持的密文信封 version={envelope.get('version')} alg={envelope.get('alg')}"
        )
    nonce = base64.urlsafe_b64decode(envelope["iv"])
    sealed = base64.urlsafe_b64decode(envelope["data"]) + base64.urlsafe_b64decode(envelope["tag"])
    return json.loads(_open(keys, envelope.get("kid"), nonce, sealed).decode("utf-8"))


def _open(keys: dict[str, bytes], kid: str | None, nonce: bytes, sealed: bytes) -> bytes:
    candidates = []
    if kid and kid in keys:
        candidates.append(keys[kid])
    candidates.extend(key for key_id, key in keys.items() if key_id != kid)
    last_error: Exception | None = None
    for key in candidates:
        try:
            return AESGCM(key).decrypt(nonce, sealed, None)
        except Exception as exc:  # noqa: BLE001 — 换下一把密钥再试
            last_error = exc
    raise ValueError(f"无法解密会话（共试了 {len(candidates)} 把密钥）") from last_error


@dataclass
class AuthSession:

    session_id: str
    user_id: str
    user_name: str | None
    credential: Credential
    created_at: float
    updated_at: float
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "user_name": self.user_name,
            "credential": self.credential.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "extra": self.extra,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AuthSession":
        return cls(
            session_id=str(data.get("session_id") or ""),
            user_id=str(data.get("user_id") or ""),
            user_name=data.get("user_name"),
            credential=Credential.from_dict(data.get("credential")),
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
            extra=data.get("extra") if isinstance(data.get("extra"), dict) else {},
        )

    def public_view(self) -> dict[str, Any]:
        return {
            "sessionId": self.session_id,
            "userId": self.user_id,
            "userName": self.user_name,
            "createdAt": self.created_at,
        }


class AuthSessionStore:

    def __init__(self, ttl_s: float = DEFAULT_SESSION_TTL_S, persist: bool = True) -> None:
        self._ttl_s = ttl_s
        self._persist = persist
        self._lock = threading.RLock()
        self._by_session: dict[str, AuthSession] = {}
        self._by_user: dict[str, str] = {}
        self._loaded = False
        self._loaded_mtime: int | None = None

    def get(self, session_id: str) -> AuthSession | None:
        with self._lock:
            self._ensure_loaded()
            session = self._by_session.get(session_id)
            if session and self._is_stale(session):
                self._drop(session)
                return None
            return session

    def get_by_user(self, user_id: str) -> AuthSession | None:
        with self._lock:
            self._ensure_loaded()
            session_id = self._by_user.get(user_id)
            return self.get(session_id) if session_id else None

    def any_session(self) -> AuthSession | None:
        with self._lock:
            self._ensure_loaded()
            for session in list(self._by_session.values()):
                if not self._is_stale(session):
                    return session
            return None

    def list_sessions(self) -> list[AuthSession]:
        with self._lock:
            self._ensure_loaded()
            return [s for s in self._by_session.values() if not self._is_stale(s)]

    def create(self, outcome: LoginOutcome) -> AuthSession:
        now = time.time()
        session = AuthSession(
            session_id=secrets.token_urlsafe(24),
            user_id=outcome.user_id,
            user_name=outcome.user_name,
            credential=outcome.credential,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._ensure_loaded(force=True)
            # 同一账号重复登录：旧会话直接顶掉，避免残留多份凭据。
            old_id = self._by_user.get(session.user_id)
            if old_id:
                self._by_session.pop(old_id, None)
            self._by_session[session.session_id] = session
            self._by_user[session.user_id] = session.session_id
            self._flush()
        logger.info(
            "[Auth] 会话已建立 user=%s session=%s", mask(session.user_id), mask(session.session_id)
        )
        return session

    def update_credential(self, session_id: str, credential: Credential) -> AuthSession | None:
        with self._lock:
            # 版本栅栏：先同步磁盘。另一个进程可能已经登出了，内存里那份是陈的——
            # 不同步就会把已撤销的会话连同新凭据一起写回去（"注销后又复活"）。
            self._ensure_loaded(force=True)
            session = self._by_session.get(session_id)
            if session is None:
                logger.info("[Auth] 会话已不存在，丢弃迟到的凭据更新 session=%s", mask(session_id))
                return None
            session.credential = credential
            session.updated_at = time.time()
            self._flush()
            return session

    def remove(self, session_id: str) -> None:
        with self._lock:
            # 先同步磁盘，理由同 update_credential：落盘写的是整份内存快照，内存是陈的
            # 就会把另一个进程刚写的东西（比如刚续好的凭据）盖回旧的。
            self._ensure_loaded(force=True)
            session = self._by_session.get(session_id)
            if session:
                self._drop(session)

    def clear(self) -> None:
        with self._lock:
            self._ensure_loaded(force=True)
            self._by_session.clear()
            self._by_user.clear()
            self._flush()

    def _is_stale(self, session: AuthSession) -> bool:
        return time.time() - session.created_at > self._ttl_s

    def _drop(self, session: AuthSession) -> None:
        self._by_session.pop(session.session_id, None)
        if self._by_user.get(session.user_id) == session.session_id:
            self._by_user.pop(session.user_id, None)
        self._flush()

    @staticmethod
    def _session_file() -> Path:
        return auth_dir() / _SESSION_FILE_NAME

    def _ensure_loaded(self, *, force: bool = False) -> None:
        """从存档同步会话。

        jiuwenswarm 是**多进程**的：Gateway 负责登录并写存档，AgentServer 另一个
        进程要读到同一份登录态。所以这里不能只加载一次——按存档 mtime 判断，
        文件变了就重新加载，否则登录前启动的进程永远看不到登录。写操作前强制
        读取一次，避免文件系统时间戳粒度不足时用旧快照覆盖其他进程的更新。
        """
        if not self._persist:
            self._loaded = True
            return
        path = self._session_file()
        if not path.exists():
            self._loaded = True
            return
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            mtime = None
        if not force and self._loaded:
            if mtime is not None and mtime == self._loaded_mtime:
                return
        self._loaded = True
        self._loaded_mtime = mtime
        self._by_session.clear()
        self._by_user.clear()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            version = raw.get("v") if isinstance(raw, dict) else None
            if version != _ARCHIVE_VERSION:
                logger.info("[Auth] 会话存档格式认不出（v=%s），按未登录处理", version)
                return
            records = decrypt_json(raw["payload"])
        except Exception as exc:  # noqa: BLE001 — 存档损坏不该拖垮启动
            logger.warning("[Auth] 会话存档不可用，按未登录处理: %s", exc)
            return
        for record in records if isinstance(records, list) else []:
            try:
                session = AuthSession.from_dict(record)
            except (TypeError, ValueError):
                continue
            if not session.session_id or self._is_stale(session):
                continue
            self._by_session[session.session_id] = session
            self._by_user[session.user_id] = session.session_id
        if self._by_session:
            logger.info("[Auth] 已从存档恢复 %d 个登录会话", len(self._by_session))

    def _flush(self) -> None:
        if not self._persist:
            return
        path = self._session_file()
        try:
            payload = encrypt_json([s.to_dict() for s in self._by_session.values()])
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"v": _ARCHIVE_VERSION, "payload": payload}), encoding="utf-8")
            _restrict(tmp)
            tmp.replace(path)
            # 记下自己写出的 mtime：这不是外部变更，不该触发重新加载
            try:
                self._loaded_mtime = path.stat().st_mtime_ns
            except OSError:
                self._loaded_mtime = None
        except Exception as exc:  # noqa: BLE001 — 落盘失败不影响本次登录
            logger.warning("[Auth] 会话落盘失败（仅内存生效）: %s", exc)


_store: AuthSessionStore | None = None
_store_lock = threading.Lock()


def get_session_store() -> AuthSessionStore:
    """进程级单例。"""
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = AuthSessionStore()
    return _store


def reset_session_store_for_test(store: AuthSessionStore | None = None) -> None:
    """测试用：替换/清空单例。"""
    global _store
    with _store_lock:
        _store = store
