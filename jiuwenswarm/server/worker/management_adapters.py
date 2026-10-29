# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Allowlisted management adapter factories; ordinary execution imports none."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from jiuwenswarm.common.schema.message import ReqMethod

_PROVIDERS = {
    "voice": (
        "jiuwenswarm.server.runtime.video_task.server_adapter",
        "VoiceTaskServerAdapter",
        frozenset(
            {
                ReqMethod.VOICE_TASK_CHECKPOINT_ACK.value,
                ReqMethod.VOICE_TASK_FILES.value,
            }
        ),
    ),
    "session": (
        "jiuwenswarm.server.runtime.gateway_adapter.session_adapter",
        "SessionAdapter",
        frozenset(
            {
                ReqMethod.SESSION_LIST.value,
                ReqMethod.SESSION_GET_METADATA.value,
                ReqMethod.SESSION_PIN.value,
                ReqMethod.SESSION_COLOR_SET.value,
                ReqMethod.SESSION_PREVIEW.value,
                ReqMethod.HISTORY_LIST_TURNS.value,
                ReqMethod.SESSION_RESTORE_FILES.value,
                ReqMethod.SESSION_DELETE.value,
                ReqMethod.SESSION_RENAME.value,
            }
        ),
    ),
    "workspace": (
        "jiuwenswarm.server.runtime.gateway_adapter.workspace_file_adapter",
        "WorkspaceFileAdapter",
        frozenset(
            {
                ReqMethod.MEDIA_PERSIST.value,
                ReqMethod.MEDIA_DISCARD.value,
                ReqMethod.DOCUMENT_PERSIST.value,
                ReqMethod.DOCUMENT_FORMATS.value,
                ReqMethod.PATH_SELECT_DIRECTORY.value,
                ReqMethod.PATH_SELECT_FILES.value,
                ReqMethod.FILE_IMPORT_URL.value,
                ReqMethod.FILE_UPLOAD_CHUNK.value,
                ReqMethod.FILE_DOWNLOAD_VERIFIED_CHUNK.value,
                ReqMethod.IM_FILE_PERSIST.value,
            }
        ),
    ),
    "memory": (
        "jiuwenswarm.server.runtime.gateway_adapter.memory_adapter",
        "MemoryAdapter",
        frozenset(
            {
                ReqMethod.MEMORY_LIST.value,
                ReqMethod.MEMORY_EDIT.value,
                ReqMethod.MEMORY_STATUS.value,
                ReqMethod.MEMORY_TOGGLE.value,
                ReqMethod.MEMORY_OPEN.value,
            }
        ),
    ),
    "project": (
        "jiuwenswarm.server.runtime.gateway_adapter.project_adapter",
        "ProjectAdapter",
        frozenset(
            {
                ReqMethod.PROJECT_INFO.value,
                ReqMethod.PROJECT_PINNED_SESSIONS.value,
                ReqMethod.PROJECT_GET_SESSIONS.value,
                ReqMethod.PROJECT_GET_CRON_SESSIONS.value,
                ReqMethod.PROJECT_CRON_RESOLVE_BINDING.value,
                ReqMethod.PROJECT_LIST.value,
                ReqMethod.PROJECT_CREATE.value,
                ReqMethod.PROJECT_RENAME.value,
                ReqMethod.PROJECT_PIN.value,
                ReqMethod.PROJECT_REMOVE.value,
                ReqMethod.PROJECT_RESTORE.value,
                ReqMethod.PROJECT_GIT_STATUS.value,
                ReqMethod.PROJECT_GIT_PROBE.value,
                ReqMethod.PROJECT_GIT_INIT.value,
                ReqMethod.PROJECT_GIT_SWITCH_BRANCH.value,
                ReqMethod.PROJECT_GIT_CREATE_BRANCH.value,
                ReqMethod.PROJECT_GIT_COMMIT.value,
                ReqMethod.PROJECT_GIT_PUSH.value,
                ReqMethod.PROJECT_GIT_DIFF_STATUS.value,
                ReqMethod.PROJECT_GIT_TURN_DIFF_LIST.value,
                ReqMethod.PROJECT_GIT_TURN_DIFF.value,
                ReqMethod.PROJECT_GIT_DISCARD_TURN_CHANGES.value,
                ReqMethod.PROJECT_GIT_REDO_TURN_CHANGES.value,
            }
        ),
    ),
    "harmonyos": (
        "jiuwenswarm.server.runtime.gateway_adapter.harmonyos_adapter",
        "HarmonyOSAdapter",
        frozenset(
            {ReqMethod.HARMONYOS_PROJECT_INIT.value, ReqMethod.HARMONYOS_DEV_INIT.value}
        ),
    ),
    "config": (
        "jiuwenswarm.server.runtime.gateway_adapter.config_adapter",
        "ConfigAdapter",
        frozenset(
            {
                ReqMethod.CONFIG_GET.value,
                ReqMethod.CONFIG_SET.value,
                ReqMethod.CONFIG_SAVE_ALL.value,
                ReqMethod.CONFIG_VALIDATE_MODEL.value,
                ReqMethod.MODELS_LIST.value,
                ReqMethod.MODELS_ZEN_ENTRIES.value,
                ReqMethod.MODELS_ZEN_WARM.value,
                ReqMethod.MODELS_GET.value,
                ReqMethod.MODELS_REPLACE_ALL.value,
                ReqMethod.MODELS_VALIDATE.value,
                ReqMethod.MODELS_UPSERT.value,
                ReqMethod.MODELS_DELETE.value,
                ReqMethod.MODELS_REFERENCES.value,
                ReqMethod.MODEL_GROUPS_LIST.value,
                ReqMethod.MODEL_GROUPS_UPSERT.value,
                ReqMethod.MODEL_GROUPS_DELETE.value,
                ReqMethod.SESSION_SELECTION_SET.value,
                ReqMethod.LOCALE_GET_CONF.value,
                ReqMethod.LOCALE_SET_CONF.value,
                ReqMethod.COMMAND_MODEL.value,
                ReqMethod.PATH_GET.value,
                ReqMethod.PATH_SET.value,
                ReqMethod.PERMISSIONS_OWNER_SCOPES_GET.value,
                ReqMethod.PERMISSIONS_OWNER_SCOPES_SET.value,
                ReqMethod.MEMORY_FORBIDDEN_GET.value,
                ReqMethod.MEMORY_FORBIDDEN_SET.value,
            }
        ),
    ),
}

_PROVIDERS["designer"] = (
    "jiuwenswarm.server.runtime.gateway_adapter.designer_adapter",
    "DesignerAdapter",
    frozenset(method.value for method in ReqMethod if method.value.startswith("designer.")),
)

_METHOD_PROVIDERS = {
    method: key for key, (_, _, methods) in _PROVIDERS.items() for method in methods
}


class LazyAdapterRegistry:
    """Create each provider once, only after an explicit supported RPC lookup."""

    def __init__(self, host: Any) -> None:
        self._host = host
        self._instances: dict[str, Any] = {}
        self._overrides: dict[str, Any] = {}

    def get(self, method: str | None) -> Any:
        if method in self._overrides:
            return self._overrides[method]
        key = _METHOD_PROVIDERS.get(method)
        if key is None:
            return None
        if key not in self._instances:
            module_name, class_name, _ = _PROVIDERS[key]
            factory = getattr(import_module(module_name), class_name)
            kwargs = {}
            if key == "project":
                kwargs["runtime_probe"] = self._host._execution_runtime
            elif key == "config":
                kwargs["on_config_saved"] = self._host._reload_after_config_save
            # Failed construction is intentionally not cached.
            self._instances[key] = factory(**kwargs)
        return self._instances[key]

    def register(self, adapter: Any) -> None:
        self._overrides.update({method: adapter for method in adapter.methods})

    def contains(self, method: str | None) -> bool:
        return method in self._overrides or method in _METHOD_PROVIDERS

    def methods(self) -> frozenset[str]:
        return frozenset(_METHOD_PROVIDERS) | frozenset(self._overrides)
