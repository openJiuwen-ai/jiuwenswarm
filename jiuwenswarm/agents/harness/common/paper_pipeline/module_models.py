"""Per-module model override for the agent-core paper pipeline (``--module-model reporting=<model>``).

Every module resolves its model name with ``MODEL_NAME`` from the environment taking precedence over
any per-module config key (``reporting.model`` in the YAML is shadowed by the environment), so the
whole pipeline runs on one model. The modules differ in what they need: writing the paper decides
most of what a reviewer sees, while routing, design and code repair run fine on a cheaper model.

``install_module_models`` wraps the ``_setting`` resolver of the class-based modules so that, for the
listed modules only, the model name comes from the override; credentials and endpoint are unchanged
(the override model must be served by the same ``API_BASE``). The usage ledger records the model of
every call, so ``budget_guard`` prices each call at its own model's rate.
"""

from __future__ import annotations

import importlib

_BASE = "openjiuwen.rsi.artifact_rsi.paper_opt.auto_research.modules"
# module id -> (python module, class) for modules that resolve their model via ``self._setting``
SETTING_MODULES = {
    "reporting": ("reporting.agent", "ReportingAgent"),
    "reflection": ("reflection.agent", "ReflectionAgent"),
    "code_implementation": ("code_implementation.agent", "CodeImplementationAgent"),
}


def parse_module_models(spec: str) -> dict[str, str]:
    """``"reporting=deepseek-v4-pro,reflection=x"`` -> mapping; rejects unknown modules."""
    mapping: dict[str, str] = {}
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        module, sep, model = part.partition("=")
        module, model = module.strip(), model.strip()
        if not sep or not model:
            raise ValueError(f"--module-model expects <module>=<model>, got {part!r}")
        if module not in SETTING_MODULES:
            raise ValueError(f"--module-model: unsupported module {module!r}; choose from {sorted(SETTING_MODULES)}")
        mapping[module] = model
    return mapping


def install_module_models(mapping: dict[str, str]) -> dict[str, str]:
    """Patch the listed modules' ``_setting`` (idempotent; re-install replaces the model)."""
    for module, model in mapping.items():
        path, cls_name = SETTING_MODULES[module]
        cls = getattr(importlib.import_module(f"{_BASE}.{path}"), cls_name)
        original = cls.__dict__["_setting"]
        original = getattr(original, "__module_model_wrapped__", original)

        def _setting(self, config_key, env_key, *args, _original=original, _model=model, **kwargs):
            if config_key == "model":
                return _model
            return _original(self, config_key, env_key, *args, **kwargs)

        _setting.__module_model_wrapped__ = original  # type: ignore[attr-defined]
        _setting.__module_model__ = model  # type: ignore[attr-defined]
        setattr(cls, "_setting", _setting)
    return dict(mapping)
