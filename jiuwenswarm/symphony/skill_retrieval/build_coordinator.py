from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from .config import load_settings
from .index_service import SkillIndexService


@dataclass
class _BuildTask:
    thread: threading.Thread
    cancel_event: threading.Event
    force: bool
    source: str


_TASKS: dict[str, _BuildTask] = {}
_LOCK = threading.RLock()


def has_active_skill_index_build() -> bool:
    settings = load_settings()
    key = str(settings.artifact_root)
    with _LOCK:
        task = _TASKS.get(key)
        if task is None:
            return False
        if task.thread.is_alive() and not task.cancel_event.is_set():
            return True
        _TASKS.pop(key, None)
        return False


def start_skill_index_build(
    manager: Any,
    *,
    force: bool = False,
    source: str = "manual",
) -> dict[str, Any]:
    settings = load_settings()
    key = str(settings.artifact_root)
    service = SkillIndexService(manager)
    if not settings.enabled:
        return {"success": False, "result": service.build_index(force=force, source=source).get("result", "")}

    with _LOCK:
        existing = _TASKS.get(key)
        if existing is not None and existing.thread.is_alive() and not existing.cancel_event.is_set():
            return {
                "success": True,
                "background": True,
                "build_status": "running",
                "result": (
                    "# Skill Index Build\n\n"
                    "A skill index build is already running in the background. "
                    "Open the Skill Index tab to watch progress."
                ),
            }
        if existing is not None:
            _TASKS.pop(key, None)

        cancel_event = threading.Event()
        service.mark_background_started(source=source)
        thread = threading.Thread(
            target=_run_build,
            args=(manager, key, force, source, cancel_event),
            name="skill-index-build",
            daemon=True,
        )
        _TASKS[key] = _BuildTask(thread=thread, cancel_event=cancel_event, force=force, source=source)
        thread.start()

    return {
        "success": True,
        "background": True,
        "build_status": "running",
        "result": (
            "# Skill Index Build\n\n"
            "Skill index build started in the background. "
            "Open the Skill Index tab to watch progress."
        ),
    }


def build_skill_index_and_wait(
    manager: Any,
    *,
    force: bool = False,
    source: str = "tool",
) -> dict[str, Any]:
    """Start or reuse the shared background build, then wait for its final state."""
    started = start_skill_index_build(manager, force=force, source=source)
    if not started.get("background"):
        return started

    settings = load_settings()
    key = str(settings.artifact_root)
    while True:
        with _LOCK:
            task = _TASKS.get(key)
        if task is None:
            break
        task.thread.join(timeout=0.2)
        if not task.thread.is_alive():
            break
    return _build_result_from_status(manager)


def cancel_skill_index_build(manager: Any) -> dict[str, Any]:
    settings = load_settings()
    key = str(settings.artifact_root)
    with _LOCK:
        task = _TASKS.get(key)
        if task is not None and task.thread.is_alive():
            task.cancel_event.set()
            SkillIndexService(manager).request_cancel()
            _TASKS.pop(key, None)
            return {
                "success": True,
                "build_status": "cancelled",
                "result": "# Skill Index Build\n\nCancellation requested.",
            }

    return {
        "success": False,
        "build_status": "idle",
        "result": "# Skill Index Build\n\nNo running skill index build was found.",
    }


def _run_build(
    manager: Any,
    key: str,
    force: bool,
    source: str,
    cancel_event: threading.Event,
) -> None:
    try:
        SkillIndexService(manager).build_index(
            force=force,
            source=source,
            cancel_check=cancel_event.is_set,
        )
    finally:
        with _LOCK:
            task = _TASKS.get(key)
            if task is not None and task.cancel_event is cancel_event:
                _TASKS.pop(key, None)


def _build_result_from_status(manager: Any) -> dict[str, Any]:
    status = SkillIndexService(manager).status()
    build_status = str(status.get("build_status") or "idle")
    if build_status == "success" or (status.get("index_exists") and status.get("fresh")):
        return {
            "success": True,
            "build_status": build_status,
            "result": (
                "# Skill Index Build\n\n"
                "Skill index build completed. You can now call `skill_branch_explore` "
                "or `skill_branch_peek` to inspect installed skills."
            ),
        }
    if build_status == "cancelled":
        return {
            "success": False,
            "build_status": build_status,
            "result": "# Skill Index Build\n\nSkill index build was cancelled.",
        }
    reason = str(status.get("build_error") or status.get("build_message") or "Skill index build did not complete.")
    return {
        "success": False,
        "build_status": build_status,
        "result": f"# Skill Index Build\n\nSkill index build failed.\n\n{reason}",
    }
