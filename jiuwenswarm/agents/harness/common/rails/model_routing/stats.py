"""model_routing.stats — per-model token usage persistence."""
from __future__ import annotations
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext

from jiuwenswarm.common.utils import logger


class _ModelUsageStats:
    """持久化「可选模型表 + 累积 token 用量」到一个 JSON 文件（跨会话）。

    结构：{"models": {<model_id>: {"model_name","model_provider","model_group",
    "is_trusted","model_type","token_used":{"input_tokens","output_tokens",
    "call_count","last_used"}}}, "updated": <iso>}
    config 仍是模型清单来源；本文件是表快照 + token_used 累积，加载时合并+回写。
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        override = os.getenv("JIUWENSWARM_MODEL_ROUTING_STATS_PATH", "").strip()
        self._path = Path(override) if override else (
            path or self._default_path()
        )
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {"models": {}}
        self._load()

    @staticmethod
    def _default_path() -> Path:
        try:
            from jiuwenswarm.common.utils import get_config_dir
            d = get_config_dir() / "routing_state"
            d.mkdir(parents=True, exist_ok=True)
            return d / "model_routing_list.json"
        except Exception:
            return Path.home() / ".jiuwenswarm" / "routing_state" / "model_routing_list.json"

    def _load(self) -> None:
        try:
            if self._path.exists():
                raw = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and isinstance(raw.get("models"), dict):
                    self._data = raw
                    self._migrate_entries()
        except Exception as exc:
            logger.debug("[ModelRouting] stats load failed: %s", exc)
            self._data = {"models": {}}

    def _migrate_entries(self) -> None:
        """旧格式（flat input_tokens/output_tokens/call_count/last_used）→ 嵌套 token_used."""
        models = self._data.get("models")
        if not isinstance(models, dict):
            return
        for entry in models.values():
            if not isinstance(entry, dict) or "token_used" in entry:
                continue
            entry["token_used"] = {
                "input_tokens": int(entry.pop("input_tokens", 0) or 0),
                "output_tokens": int(entry.pop("output_tokens", 0) or 0),
                "call_count": int(entry.pop("call_count", 0) or 0),
                "last_used": entry.pop("last_used", None),
            }

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._data["updated"] = datetime.now(tz=timezone.utc).isoformat()
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self._path)
        except Exception as exc:
            logger.debug("[ModelRouting] stats save failed: %s", exc)

    def record(
        self,
        model_id: str,
        model_name: str,
        input_tokens: int,
        output_tokens: int,
        *,
        model_provider: str = "unknown",
        model_group: str = "unknown",
        is_trusted: bool = False,
    ) -> None:
        if not model_id:
            model_id = model_name or "unknown"
        with self._lock:
            entry = self._data["models"].setdefault(
                model_id,
                {
                    "model_name": model_name,
                    "model_provider": model_provider,
                    "model_group": model_group,
                    "is_trusted": is_trusted,
                    "token_used": {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "call_count": 0,
                        "last_used": None,
                    },
                },
            )
            entry["model_name"] = model_name or entry.get("model_name", "")
            entry["model_provider"] = model_provider
            entry["model_group"] = model_group
            entry["is_trusted"] = is_trusted
            tu = entry.setdefault(
                "token_used",
                {"input_tokens": 0, "output_tokens": 0,
                 "call_count": 0, "last_used": None},
            )
            tu["input_tokens"] = int(tu.get("input_tokens", 0)) + int(input_tokens)
            tu["output_tokens"] = int(tu.get("output_tokens", 0)) + int(output_tokens)
            tu["call_count"] = int(tu.get("call_count", 0)) + 1
            tu["last_used"] = datetime.now(tz=timezone.utc).isoformat()
            self._save()

    def persist_table(self, caps: list) -> None:
        """把当前能力表快照（可序列化字段 + 累积 token_used）合并写回文件。

        保留文件里已有但不在当前 caps 的模型（加模型不清他人统计；删模型暂留统计供再加）。
        caps 里的模型：更新表字段 + 保留累积 token_used；caps 外的：原样保留。
        """
        with self._lock:
            old = self._data.get("models", {})
            new_models: dict[str, Any] = dict(old)  # 保留已有，不丢
            for cap in caps:
                key = cap.model_id or cap.model_name
                ex = old.get(key, {})
                if not isinstance(ex, dict):
                    ex = {}
                tu = ex.get("token_used")
                if not isinstance(tu, dict):
                    tu = {
                        "input_tokens": int(ex.get("input_tokens", 0) or 0),
                        "output_tokens": int(ex.get("output_tokens", 0) or 0),
                        "call_count": int(ex.get("call_count", 0) or 0),
                        "last_used": ex.get("last_used"),
                    }
                new_models[key] = {
                    "model_name": cap.model_name,
                    "model_provider": cap.model_provider,
                    "model_group": cap.model_group,
                    "is_trusted": cap.is_trusted,
                    "model_type": getattr(cap, "model_type", "") or "",
                    "max_length": int(cap.max_length),
                    "model_performance": int(cap.model_performance),
                    "model_score": int(cap.model_score),
                    "model_cost": int(cap.model_cost),
                    "model_expertise_category": (
                        list(cap.model_expertise_category)
                        if isinstance(cap.model_expertise_category, (list, tuple))
                        else []
                    ),
                    "token_used": tu,
                }
            self._data["models"] = new_models
            self._save()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data, ensure_ascii=False))


_STATS_SINGLETON: Optional[_ModelUsageStats] = None
_STATS_SINGLETON_LOCK = threading.Lock()


def get_stats_store(path: Optional[str] = None) -> _ModelUsageStats:
    """进程级单例统计存储（所有 rail 实例共享，一个文件一把锁）。

    首次调用可传 ``path`` 指定统计文件路径（来自 config.yaml ``model_routing.stats_path``）；
    后续调用忽略 path（单例已建）。
    """
    global _STATS_SINGLETON
    if _STATS_SINGLETON is None:
        with _STATS_SINGLETON_LOCK:
            if _STATS_SINGLETON is None:
                _STATS_SINGLETON = _ModelUsageStats(Path(path) if path else None)
    return _STATS_SINGLETON


def reset_stats_store_for_test(path: Optional[str] = None) -> _ModelUsageStats:
    """测试用：重置单例并指向指定路径。"""
    global _STATS_SINGLETON
    with _STATS_SINGLETON_LOCK:
        _STATS_SINGLETON = _ModelUsageStats(Path(path) if path else None)
    return _STATS_SINGLETON


# --------------------------------------------------------------------------- #
# 分类器（dev：大模型 prompt 代替 1.5B）
# --------------------------------------------------------------------------- #
Classifier = Callable[[str], Any]
