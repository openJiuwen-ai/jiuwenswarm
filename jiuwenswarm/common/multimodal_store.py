# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Catalogue storage using the existing settings lock and credential provider."""

from copy import deepcopy
from io import StringIO
import os
import re
from pathlib import Path

from dotenv import dotenv_values
from ruamel.yaml import YAML

from jiuwenswarm.common.config import config_write_lock, load_yaml_round_trip
from jiuwenswarm.common.config_persistence import commit_multimodal, publish_environment
from jiuwenswarm.common.multimodal_models import (
    CAPABILITIES, MultimodalError, normalize_legacy_model, public_snapshot, references, transition, validate_catalogue,
)
from jiuwenswarm.common.multimodal_projection import ALIASES, FIELDS, migrate_catalogue, model_block, project_catalogue, resolve_legacy
from jiuwenswarm.common.security.base_crypto import get_crypto_provider
from jiuwenswarm.common.utils import get_config_file


def _credentials(value, *, encrypt: bool, secret: bool = False, environment_references: bool = False):
    crypto = get_crypto_provider()
    if crypto is None:
        return deepcopy(value)
    if isinstance(value, dict):
        return {key: _credentials(item, encrypt=encrypt, environment_references=environment_references,
                                 secret=("api_key" in key.lower() or "token" in key.lower())
                                 and not key.lower().endswith("_tokens"))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_credentials(item, encrypt=encrypt, secret=secret, environment_references=environment_references) for item in value]
    if secret and isinstance(value, str) and value:
        if environment_references and re.fullmatch(r"\$\{[A-Z_]+\}", value):
            return value
        try:
            return crypto.encrypt(value) if encrypt else crypto.decrypt(value)
        except Exception:
            raise MultimodalError("MM_CONFIG_READ_FAILED") from None
    return value


def _yaml_bytes(data: dict) -> bytes:
    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.default_flow_style = False
    yaml.indent(mapping=2, sequence=4, offset=2)
    yaml.width = 4096
    stream = StringIO()
    yaml.dump(data, stream)
    return stream.getvalue().encode()


class MultimodalStore:
    """Resolve catalogue paths in the active user context for each operation."""

    def _load(self, path: Path, *, migrate: bool = False) -> tuple[dict, dict]:
        try:
            raw = load_yaml_round_trip(path) or {}
            state = raw.get("multimodal")
            if state is None:
                if not migrate:
                    raise MultimodalError("MM_CONFIG_READ_FAILED")
                persisted = dict(dotenv_values(path.with_name(".env")))
                env = {**os.environ, **{key: value for key, value in persisted.items() if value is not None}}
                resolved = resolve_legacy(raw, env)
                legacy_models = {"models": {tool: model_block(resolved, tool) for tool in CAPABILITIES[:3]}}
                managed_keys = {f"{tool.upper()}_{suffix}" for tool in CAPABILITIES
                                for suffix in (*FIELDS.values(), "ENABLED", *ALIASES.get(tool, ()))}
                managed_env = {key: value for key, value in env.items() if key in managed_keys}
                state = migrate_catalogue(_credentials(legacy_models, encrypt=False), _credentials(managed_env, encrypt=False))
                # Migration preserves switches and missing values. It never runs
                # create-model rules or new-form validation.
                validate_catalogue(state)
            else:
                state = _credentials(state, encrypt=False)
                validate_catalogue(state)
                # Retire the previous write-version metadata when loading old catalogues.
                state.pop("revision", None)
                # Existing catalogues expose the effective connection too. Startup
                # apply (or the next edit) persists it and removes redundant aliases.
                state["models"] = [normalize_legacy_model(model) for model in state["models"]]
            return raw, state
        except MultimodalError:
            raise
        except Exception:
            raise MultimodalError("MM_CONFIG_READ_FAILED") from None

    def _commit(self, path: Path, raw: dict, state: dict) -> dict[str, str | None]:
        projected, updates = project_catalogue(raw, state)
        # Protect authoritative and derived credentials with the same provider.
        projected["multimodal"] = _credentials(state, encrypt=True)
        for tool in CAPABILITIES[:3]:
            block = model_block(projected, tool)
            for key in ("model_config", "model_client_config", "model_config_obj"):
                if key in block:
                    block[key] = _credentials(block[key], encrypt=True, environment_references=True)
        commit_multimodal(path, _yaml_bytes(projected), _credentials(updates, encrypt=True))
        publish_environment(updates)
        return updates

    def initialize(self) -> None:
        """Commit migration and all execution fields together before consumers start."""
        path = get_config_file()
        with config_write_lock(config_path=path):
            raw, state = self._load(path, migrate=True)
            self._commit(path, raw, state)

    def read(self, model_id: str | None = None) -> dict:
        path = get_config_file()
        with config_write_lock(config_path=path):
            _, state = self._load(path)
            if model_id is None:
                return public_snapshot(state)
            model = next((m for m in state["models"] if m["model_id"] == model_id), None)
            if model is None:
                raise MultimodalError("MM_MODEL_NOT_FOUND", {"model_id": model_id})
            editable = {key: deepcopy(value) for key, value in model.items() if key != "legacy_options"}
            return {"model": editable, "references": references(state, model_id)}

    def mutate(self, operation: str, params: dict) -> tuple[dict, dict[str, str | None]]:
        path = get_config_file()
        with config_write_lock(config_path=path):
            raw, state = self._load(path)
            candidate, result = transition(state, operation, params)
            updates = {}
            if result["changed"] or operation == "config.apply":
                updates = self._commit(path, raw, candidate)
            result["snapshot"] = public_snapshot(candidate)
            result["saved"] = True
            return result, updates

def initialize_multimodal_models() -> None:
    """Migrate and project saved settings before Runtime consumers start."""
    MultimodalStore().initialize()
