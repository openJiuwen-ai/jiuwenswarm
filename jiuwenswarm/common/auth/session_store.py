# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名（auth 拆分过渡）：sessions.json 凭据存储，真源已迁
``gateway.auth.session_store``（gateway claim 写 / server
apply_credential_update 写，真源单点）。

server/agents 侧消费方经本路径取用（拍板 #10 方向：母仓反向依赖
gateway 仓）；B3 拆仓时随 shim 收敛退役。
"""

from gateway.auth.session_store import *  # noqa: F401,F403
from gateway.auth.session_store import (  # noqa: F401
    AESGCM,
    Any,
    AuthSession,
    AuthSessionStore,
    Credential,
    DEFAULT_SESSION_TTL_S,
    LoginOutcome,
    Path,
    _ARCHIVE_VERSION,
    _ENVELOPE_ALG,
    _ENVELOPE_VERSION,
    _KEY_FILE_NAME,
    _SESSION_FILE_NAME,
    _install_key,
    _key_id,
    _keyring,
    _open,
    _parse_install_key,
    _publish_new_file,
    _restrict,
    _store,
    _store_lock,
    annotations,
    auth_dir,
    base64,
    dataclass,
    decrypt_json,
    derive_local_secret,
    encrypt_json,
    field,
    get_session_store,
    get_user_workspace_dir,
    hashlib,
    json,
    logger,
    logging,
    mask,
    os,
    reset_session_store_for_test,
    secrets,
    threading,
    time,
)
