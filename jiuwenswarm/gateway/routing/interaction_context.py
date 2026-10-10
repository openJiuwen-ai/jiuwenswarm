# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""PendingInteraction — 统一的追问上下文，支持群聊追问和 DM 追问两种模式。

存储路径: {workspace_dir}/agent/workspace/interactions/{interaction_id}.json
文件名前缀: gpq_* 群聊追问, iact_* DM 追问
TTL: 24 小时
"""

from __future__ import annotations

import glob
import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import get_interactions_dir

logger = logging.getLogger(__name__)

# 凭证类"中心词"：只有当它是键名的**最后一个词段**时才判定为敏感。
# 英文复合词把中心词放在词尾：``access_token`` 是一个令牌，``token_count``
# 是一个计数。按最后一个词段判定即可区分二者。
_SENSITIVE_HEAD_TERMS = frozenset({
    "authorization",
    "bearer",
    "credential",
    "credentials",
    "passphrase",
    "passwd",
    "password",
    "pwd",
    "secret",
    "signature",
    "token",
})
# 末尾两个词段合并后命中，也判定为敏感。``key`` 单独作为中心词过宽
# （``cache_key`` / ``route_key`` 不是凭证），因此必须带一个限定词。
_SENSITIVE_TAIL_PAIRS = frozenset({
    "access_key",
    "api_key",
    "app_key",
    "client_key",
    "encryption_key",
    "private_key",
    "secret_key",
    "session_key",
    "signing_key",
})
# camelCase 边界：``accessToken`` -> ``access Token``。snake_case 与
# kebab-case 由 ``_KEY_SEPARATORS`` 切分。
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_KEY_SEPARATORS = re.compile(r"[^A-Za-z0-9]+")
_DEFAULT_TTL = 86400


def _get_interactions_dir() -> Path:
    interactions_dir = get_interactions_dir()
    interactions_dir.mkdir(parents=True, exist_ok=True)
    return interactions_dir


def _key_segments(key: str) -> list[str]:
    """把键名切成小写词段：``tenant_access_token`` -> ``["tenant", "access", "token"]``。

    分隔符为任意非字母数字字符（``_`` / ``-`` / ``.`` / 空格）。camelCase 边界
    也切分，使 ``accessToken`` 与 ``access_token`` 得到同一结果。
    """
    spaced = _CAMEL_BOUNDARY.sub(" ", key)
    return [seg for seg in _KEY_SEPARATORS.split(spaced.lower()) if seg]


def _is_sensitive_key(key: str) -> bool:
    """键名的**最后一个词段**指向一个凭证时返回 True。

    命中: ``token`` / ``bot_token`` / ``tenant_access_token`` / ``accessToken``
          / ``client_secret`` / ``authorization`` / ``api_key`` / ``x-api-key``
    不命中: ``tokenizer_cache_dir`` / ``enable_tiktoken_counter``
            / ``token_count`` / ``token_usage`` / ``cache_key``
    这些不命中的键是正常元数据。把它们也滤掉，就是用一个新缺陷换掉旧缺陷。
    """
    segments = _key_segments(key)
    if not segments:
        return False
    if segments[-1] in _SENSITIVE_HEAD_TERMS:
        return True
    return len(segments) >= 2 and "_".join(segments[-2:]) in _SENSITIVE_TAIL_PAIRS


def _filter_sensitive(metadata: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in metadata.items() if not _is_sensitive_key(str(k))}


@dataclass
class PendingInteraction:
    interaction_id: str
    mode: str
    origin_channel_id: str
    origin_session_id: str
    origin_content: str
    origin_sender_name: str
    origin_sender_id: str
    question: str
    target_user_id: str
    target_user_name: str = ""
    origin_metadata: dict[str, Any] = field(default_factory=dict)
    principal_id: str = ""
    status: str = "pending"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def _path(self) -> Path:
        return _get_interactions_dir() / f"{self.interaction_id}.json"

    def save(self) -> None:
        self.updated_at = time.time()
        data = {
            "interaction_id": self.interaction_id,
            "mode": self.mode,
            "origin_channel_id": self.origin_channel_id,
            "origin_session_id": self.origin_session_id,
            "origin_content": self.origin_content,
            "origin_sender_name": self.origin_sender_name,
            "origin_sender_id": self.origin_sender_id,
            "question": self.question,
            "target_user_id": self.target_user_id,
            "target_user_name": self.target_user_name,
            "origin_metadata": _filter_sensitive(self.origin_metadata),
            "principal_id": self.principal_id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        try:
            self._path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("[PendingInteraction] save: id=%s mode=%s", self.interaction_id, self.mode)
        except Exception as exc:
            logger.warning("[PendingInteraction] save 失败: %s", exc)

    def remove(self) -> None:
        try:
            p = self._path()
            if p.exists():
                p.unlink()
                logger.info("[PendingInteraction] remove: id=%s", self.interaction_id)
        except Exception as exc:
            logger.warning("[PendingInteraction] remove 失败: %s", exc)

    @classmethod
    def load(cls, interaction_id: str) -> "PendingInteraction | None":
        p = _get_interactions_dir() / f"{interaction_id}.json"
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return cls(**{k: data[k] for k in data if k in cls.__dataclass_fields__})
        except Exception as exc:
            logger.warning("[PendingInteraction] load 失败: id=%s %s", interaction_id, exc)
            return None

    @classmethod
    def find_pending(cls, channel_id: str, principal_id: str) -> "PendingInteraction | None":
        d = _get_interactions_dir()
        oldest: "PendingInteraction | None" = None
        for prefix in (f"iact_{channel_id}_", "gpq_"):
            pattern = str(d / f"{prefix}*.json")
            for fp in glob.glob(pattern):
                try:
                    data = json.loads(Path(fp).read_text(encoding="utf-8"))
                    pi = cls(**{k: data[k] for k in data if k in cls.__dataclass_fields__})
                    if pi.status != "pending":
                        continue
                    if time.time() - pi.created_at > _DEFAULT_TTL:
                        pi.remove()
                        continue
                    if pi.principal_id and pi.principal_id != principal_id:
                        continue
                    if pi.target_user_id and pi.target_user_id != principal_id:
                        continue
                    if oldest is None or pi.created_at < oldest.created_at:
                        oldest = pi
                except Exception as e:
                    logger.debug("[PendingInteraction] 解析交互文件 %s 失败: %s", fp, e)
                    continue
        cls.cleanup_expired()
        return oldest

    @classmethod
    def find_all_group_pending(cls) -> list["PendingInteraction"]:
        d = _get_interactions_dir()
        pattern = str(d / "gpq_*.json")
        result: list[PendingInteraction] = []
        for fp in glob.glob(pattern):
            try:
                data = json.loads(Path(fp).read_text(encoding="utf-8"))
                pi = cls(**{k: data[k] for k in data if k in cls.__dataclass_fields__})
                if pi.status == "pending" and time.time() - pi.created_at <= _DEFAULT_TTL:
                    result.append(pi)
            except Exception as e:
                logger.debug("[PendingInteraction] 解析交互文件 %s 失败: %s", fp, e)
                continue
        return result

    @classmethod
    def find_group_pending(cls, session_id: str, user_id: str) -> "PendingInteraction | None":
        interaction_id = f"gpq_{session_id}_{user_id}"
        return cls.load(interaction_id)

    @classmethod
    def cleanup_expired(cls, ttl: int = _DEFAULT_TTL) -> int:
        d = _get_interactions_dir()
        count = 0
        now = time.time()
        for fp in glob.glob(str(d / "*.json")):
            try:
                data = json.loads(Path(fp).read_text(encoding="utf-8"))
                created = float(data.get("created_at", 0))
                if now - created > ttl:
                    Path(fp).unlink()
                    count += 1
            except Exception as e:
                logger.debug("[PendingInteraction] 清理交互文件 %s 失败: %s", fp, e)
                continue
        if count:
            logger.info("[PendingInteraction] cleanup_expired: removed %d files", count)
        return count

    def build_resume_content(self, answer: str) -> str:
        if self.mode == "dm":
            return (
                f"[任务恢复] 之前的群聊请求：{self.origin_content}（来自 {self.origin_sender_name}）\n"
                f"你之前的追问：{self.question}\n"
                f"principal 的回答：{answer}\n"
                f"请综合「原始请求」和「principal 的回答」中的所有信息继续完成任务，"
                f"原始请求中已明确提供的信息直接使用即可，不要再次追问。"
                f"并将结果回复到群聊。"
            )
        return (
            f"[任务恢复] 你正在处理的群聊请求：{self.origin_content}（来自 {self.origin_sender_name}）\n"
            f"你之前追问了 {self.target_user_name or '用户'}：{self.question}\n"
            f"{self.target_user_name or '用户'} 的回答：{answer}\n"
            f"请综合「原始请求」和「用户的回答」中的所有信息来完成任务。"
            f"原始请求中已明确提供的信息（如时间、地点等）直接使用即可，不要再次追问或要求补充。"
            f"现在请立即执行任务（如设置提醒、创建日程等），不要只是确认或记录信息，并将执行结果回复到群聊。"
        )
