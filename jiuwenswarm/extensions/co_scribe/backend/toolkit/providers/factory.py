"""Pick the provider a connection's credentials describe.

A connection is one vendor and one account, and the credentials file is what says
which vendor: a Google service account key is JSON carrying ``type:
"service_account"``, while a Feishu app is an id and a secret. Reading it beats adding
a vendor field to the config, which would let the two disagree -- and the file is the
thing that actually decides what the calls can do.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.provider import ProviderError

logger = logging.getLogger(__name__)


def detect_vendor(credentials_file: str) -> str:
    """``"google"`` or ``"feishu"``, from the credential file's own shape."""
    try:
        with open(credentials_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ProviderError("invalid", f"无法读取凭证文件 {credentials_file}：{exc}") from exc
    if not isinstance(data, dict):
        raise ProviderError("invalid", f"凭证文件不是对象：{credentials_file}")
    if data.get("type") == "service_account" and data.get("client_email"):
        return "google"
    if data.get("app_id") or data.get("app_secret"):
        return "feishu"
    raise ProviderError(
        "invalid",
        f"无法判断 {credentials_file} 属于哪个厂商："
        "Google 服务账号需 type=service_account，飞书应用需 app_id/app_secret。",
    )


_ADDRESS_CACHE: dict[str, tuple[float, str]] = {}


def credential_address(credentials_file: str) -> str:
    """The address a key file's identity acts under: the service-account email for
    Google, the bot's open id for Feishu, empty when the file names neither.

    One reader for both vendors, because two readers disagreed once: the host read
    ``client_email`` alone, so a Feishu connection's own address came back empty and
    the tools could not tell a comment addressed to them from anyone else's -- the
    ``addressed`` flag never set, the chat/unattended mutex never engaged. Cached on
    the file's mtime: the tools ask on every call, and the answer changes only when
    the key does.
    """
    path = str(credentials_file or "")
    if not path:
        return ""
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return ""
    hit = _ADDRESS_CACHE.get(path)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    address = str(data.get("client_email") or data.get("bot_open_id") or "").strip()
    _ADDRESS_CACHE[path] = (mtime, address)
    return address


def build_provider(credentials_file: str, *, agent_roster: tuple[str, ...] = ()) -> Any:
    """The factory a connection registry is given.

    Failures are raised rather than returning None: a connection whose provider cannot
    be built is a configuration error someone has to see, and a silent skip would show
    up much later as a document nobody is watching.
    """
    vendor = detect_vendor(credentials_file)
    if vendor == "google":
        from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.google_provider import (
            GoogleDocsProvider,
        )

        return GoogleDocsProvider(credentials_file)

    from jiuwenswarm.extensions.co_scribe.backend.toolkit.providers.feishu_provider import (
        FeishuDocsProvider,
    )

    with open(credentials_file, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    # The roster of other agents' open_ids names the bots a mention must never treat
    # as a summoner. It is deployment policy, so the **host caller** passes it -- this
    # module stays host-free (the structure test pins that), and a host that passes
    # nothing gets the safe, unrostered default with the rate brake as backstop.
    return FeishuDocsProvider(
        profile=str(data.get("profile") or data.get("app_id") or ""),
        binary=str(data.get("lark_binary") or "lark-cli"),
        self_open_id=str(data.get("bot_open_id") or ""),
        agent_roster=tuple(agent_roster),
    )
