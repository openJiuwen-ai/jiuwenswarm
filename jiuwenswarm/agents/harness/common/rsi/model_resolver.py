"""Resolve JiuwenSwarm model selections into openjiuwen model files.

The Web ``models.list`` API exposes a selection value (model name, alias, or
``name#origin_index``).  The RSI engine, however, loads a complete
``model_client_config``/``model_request_config`` file.  This module is the
single translation boundary between those two contracts.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml

from jiuwenswarm.agents.harness.common.rsi.errors import (
    RsiModelConfigInvalid,
    RsiModelNotFound,
)


@dataclass(frozen=True, slots=True)
class ResolvedRsiModel:
    """A model entry selected from the same list as ``models.list``."""

    reference: str
    role: str
    model_name: str
    origin_index: int | None
    path: str
    config_sha256: str

    def to_manifest(self) -> dict[str, Any]:
        """Return the non-secret task manifest representation."""

        return {
            "role": self.role,
            "ref": self.reference,
            "model_name": self.model_name,
            "origin_index": self.origin_index,
            "config_sha256": self.config_sha256,
            "path": self.path,
        }


class RsiModelConfigResolver:
    """Resolve and materialize models using JiuwenSwarm's model construction.

    The loaders/builders are injectable so the selection semantics can be
    tested without booting the complete AgentServer.  In production they are
    imported lazily and are exactly the functions used by the deep adapter and
    ``models.list``.
    """

    def __init__(
        self,
        *,
        config_loader: Callable[[], dict[str, Any]] | None = None,
        defaults_loader: Callable[[dict[str, Any]], list[dict[str, Any]]] | None = None,
        zen_loader: Callable[[], list[dict[str, Any]]] | None = None,
        model_builder: Callable[[dict[str, Any], dict[str, Any]], Any] | None = None,
    ) -> None:
        self._config_loader = config_loader
        self._defaults_loader = defaults_loader
        self._zen_loader = zen_loader
        self._model_builder = model_builder

    def entries(self) -> list[tuple[dict[str, Any], int | None]]:
        """Return entries in ``models.list`` order with origin indexes.

        Configured defaults (including ``models.agentos`` entries returned by
        ``get_default_models``) receive their global index.  Zen entries are
        appended exactly as ``_models_list`` does and intentionally have no
        index because they are process-local and not part of
        ``models.defaults``.
        """

        config_loader = self._config_loader or _default_config
        defaults_loader = self._defaults_loader or _default_models
        zen_loader = self._zen_loader or _default_zen_models
        config = config_loader()
        defaults = defaults_loader(config)
        result: list[tuple[dict[str, Any], int | None]] = []
        seen_names: set[str] = set()
        for index, entry in enumerate(defaults):
            if not isinstance(entry, dict):
                continue
            result.append((entry, index))
            model_name = _model_name(entry)
            if model_name:
                seen_names.add(model_name)
        try:
            zen_entries = zen_loader()
        except Exception:
            # Zen is an optional cache.  A network/cache failure must not make
            # configured models disappear from an RSI task.
            zen_entries = []
        for entry in zen_entries or []:
            if not isinstance(entry, dict):
                continue
            model_name = _model_name(entry)
            if not model_name or model_name in seen_names:
                continue
            result.append((entry, None))
            seen_names.add(model_name)
        return result

    def resolve(self, model_ref: str) -> tuple[dict[str, Any], int | None]:
        """Resolve a reference with strict ``models.list`` semantics."""

        reference = str(model_ref or "").strip()
        if not reference:
            raise RsiModelNotFound("模型引用不能为空")
        entries = self.entries()
        if "#" in reference:
            bare_name, _, raw_index = reference.rpartition("#")
            bare_name = bare_name.strip()
            try:
                index = int(raw_index)
            except (TypeError, ValueError) as exc:
                raise RsiModelNotFound(f"模型引用 origin_index 非法: {reference}") from exc
            if not bare_name or index < 0:
                raise RsiModelNotFound(f"模型引用 origin_index 非法: {reference}")
            for entry, origin_index in entries:
                if origin_index != index:
                    continue
                if _matches(entry, bare_name):
                    return entry, origin_index
                # An index is global, so do not silently use another same-name
                # entry when the name/index pair does not agree.
                break
            raise RsiModelNotFound(f"模型引用未命中: {reference}")

        matches = [(entry, index) for entry, index in entries if _matches(entry, reference)]
        if not matches:
            raise RsiModelNotFound(f"模型引用未命中: {reference}")
        # Preserve JiuwenSwarm's pure-name behaviour: choose the explicit
        # default first, otherwise the first entry in list order.
        for entry, index in matches:
            if entry.get("is_default") is True:
                return entry, index
        return matches[0]

    def resolve_to_file(
        self,
        model_ref: str,
        role: str,
        task_models_dir: str | Path,
    ) -> dict[str, Any]:
        """Resolve a model and write the openjiuwen-readable role YAML.

        The returned mapping is deliberately safe to persist in ``task.json``
        and never contains the API key.  The private YAML file itself may
        contain the decrypted key because the current openjiuwen factory reads
        it from disk; it lives below the task directory and is not indexed as
        an artifact.
        """

        role_name = str(role or "").strip()
        if not role_name or Path(role_name).name != role_name:
            raise RsiModelConfigInvalid(f"模型 role 非法: {role}")
        entry, origin_index = self.resolve(model_ref)
        model_name = _model_name(entry)
        mcc = deepcopy(entry.get("model_client_config") or {})
        mco = deepcopy(entry.get("model_config_obj") or {})
        if not model_name or not isinstance(mcc, dict) or not isinstance(mco, dict):
            raise RsiModelConfigInvalid(f"模型条目不完整: {model_ref}")
        if not str(mcc.get("client_provider") or "").strip():
            raise RsiModelConfigInvalid(f"模型缺少 client_provider: {model_name}")
        if not str(mcc.get("api_base") or mcc.get("base_url") or "").strip():
            raise RsiModelConfigInvalid(f"模型缺少 api_base: {model_name}")

        try:
            model = (self._model_builder or _default_model_builder)(mcc, mco)
            client_data = _dump_model_part(getattr(model, "model_client_config", None))
            request_data = _dump_model_part(
                getattr(model, "model_config", getattr(model, "model_request_config", None))
            )
        except Exception as exc:  # noqa: BLE001 - normalize builder validation
            # Do not place the builder's validation repr on the wire: some
            # provider clients include the complete input mapping (including
            # a decrypted API key) in their exception text.
            raise RsiModelConfigInvalid(f"模型 {model_name} 无法构造") from exc
        if not isinstance(client_data, dict) or not isinstance(request_data, dict):
            raise RsiModelConfigInvalid(f"模型 {model_name} 构造结果无效")

        # ModelClientConfig must not retry inside the RSI engine: retry budget
        # belongs to the engine's stage policy.  The standalone member factory
        # also strips this value defensively when it loads the file.
        client_data["max_retries"] = 0
        # ``ModelRequestConfig`` currently serializes this as ``model_name``;
        # its validator accepts the public ``model`` spelling as well.  Use the
        # latter in the task file to match openjiuwen's standalone examples.
        request_data.pop("model_name", None)
        request_data["model"] = model_name
        payload = {
            "model_client_config": client_data,
            "model_request_config": request_data,
        }
        target_dir = Path(task_models_dir).expanduser().resolve()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{role_name}.yaml"
        target.write_text(
            yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        return ResolvedRsiModel(
            reference=str(model_ref).strip(),
            role=role_name,
            model_name=model_name,
            origin_index=origin_index,
            path=str(target),
            config_sha256=digest,
        ).to_manifest()

    # Alias used by callers that describe this operation as materialization.
    materialize = resolve_to_file


def select_rsi_model_entry(
    entries: list[tuple[dict[str, Any], int | None]], model_ref: str
) -> tuple[dict[str, Any], int | None]:
    """Select from a preloaded list using the resolver's strict semantics."""
    reference = str(model_ref or "").strip()
    if not reference:
        raise RsiModelNotFound("模型引用不能为空")
    if "#" in reference:
        bare_name, _, raw_index = reference.rpartition("#")
        bare_name = bare_name.strip()
        try:
            index = int(raw_index)
        except (TypeError, ValueError) as exc:
            raise RsiModelNotFound(f"模型引用 origin_index 非法: {reference}") from exc
        if not bare_name or index < 0:
            raise RsiModelNotFound(f"模型引用 origin_index 非法: {reference}")
        for entry, origin_index in entries:
            if origin_index == index and _matches(entry, bare_name):
                return entry, origin_index
        raise RsiModelNotFound(f"模型引用未命中: {reference}")

    matches = [(entry, index) for entry, index in entries if _matches(entry, reference)]
    if not matches:
        raise RsiModelNotFound(f"模型引用未命中: {reference}")
    for entry, index in matches:
        if entry.get("is_default") is True:
            return entry, index
    return matches[0]


def _matches(entry: dict[str, Any], requested: str) -> bool:
    name = _model_name(entry)
    alias = str(entry.get("alias") or "").strip()
    return requested == name or (bool(alias) and requested == alias)


def _model_name(entry: dict[str, Any]) -> str:
    mcc = entry.get("model_client_config") if isinstance(entry, dict) else None
    return str((mcc or {}).get("model_name") or "").strip() if isinstance(mcc, dict) else ""


def _dump_model_part(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    raise TypeError(f"unsupported model config object: {type(value).__name__}")


def _default_config() -> dict[str, Any]:
    from jiuwenswarm.common.config import get_config

    value = get_config()
    return value if isinstance(value, dict) else {}


def _default_models(config: dict[str, Any]) -> list[dict[str, Any]]:
    from jiuwenswarm.common.config import get_default_models

    return list(get_default_models(config))


def _default_zen_models() -> list[dict[str, Any]]:
    from jiuwenswarm.server.runtime.opencode_zen import get_zen_free_model_entries

    return list(get_zen_free_model_entries())


def _default_model_builder(mcc: dict[str, Any], mco: dict[str, Any]) -> Any:
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        build_model_from_entry,
    )

    return build_model_from_entry(mcc, mco)


__all__ = [
    "ResolvedRsiModel",
    "RsiModelConfigResolver",
    "select_rsi_model_entry",
]
