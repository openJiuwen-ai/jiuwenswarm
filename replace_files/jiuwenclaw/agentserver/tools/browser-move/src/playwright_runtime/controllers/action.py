#!/usr/bin/env python
# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Runtime-native custom action controller for Playwright runtime.

It exposes a lightweight action registry consumed by the MCP wrapper.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import json
from typing import Any, Awaitable, Callable, Protocol

from openjiuwen.core.common.logging import logger
from playwright_runtime.controllers.base import BaseController

ActionResult = dict[str, Any]
ActionHandler = Callable[..., Awaitable[Any] | Any]


class RuntimeRunner(Protocol):
    async def __call__(
        self,
        *,
        task: str,
        session_id: str | None = None,
        request_id: str | None = None,
        timeout_s: int | None = None,
    ) -> dict[str, Any]:
        ...


_ACTIONS: dict[str, ActionHandler] = {}
_ACTION_SPECS: dict[str, dict[str, Any]] = {}
_RUNTIME_RUNNER: RuntimeRunner | None = None
_LOCK = asyncio.Lock()


class ActionController(BaseController):
    """Instance-scoped registry and dispatcher for custom actions."""

    def __init__(
        self,
        *,
        actions: dict[str, ActionHandler] | None = None,
        action_specs: dict[str, dict[str, Any]] | None = None,
        runtime_runner: RuntimeRunner | None = None,
        lock: asyncio.Lock | None = None,
    ) -> None:
        self._actions: dict[str, ActionHandler] = actions if actions is not None else {}
        self._action_specs: dict[str, dict[str, Any]] = action_specs if action_specs is not None else {}
        self._runtime_runner: RuntimeRunner | None = runtime_runner
        self._lock: asyncio.Lock = lock if lock is not None else asyncio.Lock()

    def bind_runtime(self, runtime: Any) -> None:
        run_browser_task = getattr(runtime, "run_browser_task", None)
        if run_browser_task is None or not callable(run_browser_task):
            raise ValueError("runtime must expose an async run_browser_task(...) method")

        async def _runner(
            *,
            task: str,
            session_id: str | None = None,
            request_id: str | None = None,
            timeout_s: int | None = None,
        ) -> dict[str, Any]:
            return await run_browser_task(
                task=task,
                session_id=session_id,
                request_id=request_id,
                timeout_s=timeout_s,
            )

        self.bind_runtime_runner(_runner)

    def bind_runtime_runner(self, runner: RuntimeRunner | None) -> None:
        self._runtime_runner = runner

    def clear_runtime_runner(self) -> None:
        self.bind_runtime_runner(None)

    def register_action(self, name: str, handler: ActionHandler, *, overwrite: bool = True) -> None:
        action_name = _normalize_action_name(name)
        if not action_name:
            raise ValueError("action name must be non-empty")
        if not callable(handler):
            raise TypeError("handler must be callable")
        if not overwrite and action_name in self._actions:
            raise ValueError(f"action already exists: {action_name}")
        self._actions[action_name] = handler

    def register_action_spec(
        self,
        name: str,
        *,
        summary: str = "",
        when_to_use: str = "",
        params: dict[str, str] | None = None,
    ) -> None:
        action_name = _normalize_action_name(name)
        if not action_name:
            raise ValueError("action name must be non-empty")
        self._action_specs[action_name] = {
            "summary": (summary or "").strip(),
            "when_to_use": (when_to_use or "").strip(),
            "params": dict(params or {}),
        }

    def list_actions(self) -> list[str]:
        return sorted(self._actions.keys())

    def describe_actions(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for name in sorted(self._actions.keys()):
            spec = self._action_specs.get(name, {})
            result[name] = {
                "summary": str(spec.get("summary", "") or ""),
                "when_to_use": str(spec.get("when_to_use", "") or ""),
                "params": dict(spec.get("params", {}) or {}),
            }
        return result

    async def run_action(
        self,
        action: str,
        session_id: str = "",
        request_id: str = "",
        **kwargs: Any,
    ) -> ActionResult:
        action_name = _normalize_action_name(action)
        sid = (session_id or "").strip()
        rid = (request_id or "").strip()
        param_keys = ",".join(sorted(str(key) for key in kwargs.keys()))
        logger.info(
            f"CONTROLLER_ACTION start action={action_name} session_id={sid or '-'} "
            f"request_id={rid or '-'} param_keys={param_keys or '-'}"
        )

        handler = self._actions.get(action_name)
        if handler is None:
            logger.warning(
                f"CONTROLLER_ACTION unknown action={action_name} session_id={sid or '-'} request_id={rid or '-'}"
            )
            return {
                "ok": False,
                "action": action_name,
                "session_id": sid,
                "request_id": rid,
                "error": f"unknown action: {action_name}",
            }

        try:
            async with self._lock:
                raw = await _maybe_await(handler(session_id=sid, request_id=rid, **kwargs))

            response = dict(raw) if isinstance(raw, dict) else {"result": raw}
            response.setdefault("ok", True)
            response.setdefault("action", action_name)
            response.setdefault("session_id", sid)
            response.setdefault("request_id", rid)
            response.setdefault("error", None)
            logger.info(
                f"CONTROLLER_ACTION end action={action_name} session_id={sid or '-'} "
                f"request_id={rid or '-'} ok={bool(response.get('ok', False))}"
            )
            return response
        except Exception as exc:
            logger.error(
                f"CONTROLLER_ACTION error action={action_name} session_id={sid or '-'} "
                f"request_id={rid or '-'} error={exc}"
            )
            return {
                "ok": False,
                "action": action_name,
                "session_id": sid,
                "request_id": rid,
                "error": str(exc),
            }

    def register_example_actions(self) -> None:
        register_example_actions(controller=self)

    def snapshot(self) -> dict[str, Any]:
        return {
            "actions": dict(self._actions),
            "action_specs": copy.deepcopy(self._action_specs),
            "runtime_runner": self._runtime_runner,
        }

    def restore(self, snapshot: dict[str, Any]) -> None:
        self._actions.clear()
        self._actions.update(dict(snapshot.get("actions", {})))
        self._action_specs.clear()
        self._action_specs.update(copy.deepcopy(dict(snapshot.get("action_specs", {}))))
        self._runtime_runner = snapshot.get("runtime_runner")


def _normalize_action_name(name: str) -> str:
    return (name or "").strip().lower()


def _extract_json_object(text: Any) -> dict[str, Any]:
    if isinstance(text, dict):
        return text
    if text is None:
        return {}

    raw = str(text).strip()
    if not raw:
        return {}

    # Common browser_run_code shape:
    #   ### Result
    #   <value>
    #   ### Ran Playwright code
    marker_result = "### Result"
    marker_ran = "### Ran Playwright code"
    if marker_result in raw and marker_ran in raw:
        start = raw.find(marker_result) + len(marker_result)
        end = raw.find(marker_ran, start)
        if end > start:
            raw = raw[start:end].strip()

    # Some tool wrappers return JSON as a quoted string.
    for _ in range(2):
        try:
            parsed = json.loads(raw)
        except Exception:
            break
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, str):
            raw = parsed.strip()
            continue
        break

    first = raw.find("{")
    last = raw.rfind("}")
    if first >= 0 and last > first:
        snippet = raw[first : last + 1]
        try:
            parsed = json.loads(snippet)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return {}


def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _normalize_offset(value: Any) -> dict[str, int] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        x = _to_int_or_none(value.get("x"))
        y = _to_int_or_none(value.get("y"))
    else:
        x = _to_int_or_none(getattr(value, "x", None))
        y = _to_int_or_none(getattr(value, "y", None))
    if x is None or y is None:
        return None
    return {"x": x, "y": y}


def _build_run_code_task(js_code: str, purpose: str) -> str:
    tool_input = json.dumps({"code": js_code}, ensure_ascii=False)
    return (
        f"Execute this browser operation: {purpose}.\n"
        "Call browser_run_code exactly once with this JSON input:\n"
        f"{tool_input}\n\n"
        "Then return your required top-level response JSON. "
        "Set its `final` field to the exact JSON result returned by browser_run_code."
    )


def _build_coordinate_script(payload: dict[str, Any]) -> str:
    payload_json = json.dumps(payload, ensure_ascii=False)
    return (
        "async (page) => {\n"
        f"  const params = {payload_json};\n"
        "  if (params.url && String(params.url).trim()) {\n"
        "    await page.goto(String(params.url).trim());\n"
        "  }\n"
        "  const getTextBox = async (query, role) => {\n"
        "    const term = String(query || '').trim().toLowerCase();\n"
        "    if (!term) return null;\n"
        "    return await page.evaluate(({ term, role }) => {\n"
        "      const all = Array.from(document.querySelectorAll('body *'));\n"
        "      const score = (el) => {\n"
        "        const text = String(el.textContent || '').trim().toLowerCase();\n"
        "        if (!text) return -1;\n"
        "        if (text === term) return 2;\n"
        "        if (text.includes(term)) return 1;\n"
        "        return -1;\n"
        "      };\n"
        "      const toVisibleBox = (el) => {\n"
        "        if (!el) return null;\n"
        "        const candidates = role === 'target' && el.parentElement ? [el.parentElement, el] : [el];\n"
        "        for (const candidate of candidates) {\n"
        "          const rect = candidate.getBoundingClientRect();\n"
        "          if (rect && rect.width > 0 && rect.height > 0) {\n"
        "            return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };\n"
        "          }\n"
        "        }\n"
        "        return null;\n"
        "      };\n"
        "      const exactMatches = all.filter((el) => score(el) === 2);\n"
        "      for (const el of exactMatches) {\n"
        "        const box = toVisibleBox(el);\n"
        "        if (box) return box;\n"
        "      }\n"
        "      const fuzzyMatches = all.filter((el) => score(el) === 1);\n"
        "      for (const el of fuzzyMatches) {\n"
        "        const box = toVisibleBox(el);\n"
        "        if (box) return box;\n"
        "      }\n"
        "      return null;\n"
        "    }, { term, role });\n"
        "  };\n"
        "  const extractTextFromHasText = (s) => {\n"
        "    if (!s || typeof s !== 'string') return s;\n"
        "    const m = String(s).match(/:has-text\\s*\\(\\s*['\"]([^'\"]*)['\"]\\s*\\)/);\n"
        "    return m ? m[1] : s;\n"
        "  };\n"
        "  const getPoint = async (selector, offset, role) => {\n"
        "    let box = null;\n"
        "    if (selector) {\n"
        "      try {\n"
        "        const el = await page.$(selector);\n"
        "        if (el) box = await el.boundingBox();\n"
        "      } catch (_err) {\n"
        "        box = null;\n"
        "      }\n"
        "    }\n"
        "    if (!box) {\n"
        "      const textTerm = extractTextFromHasText(selector) || selector;\n"
        "      box = await getTextBox(textTerm, role);\n"
        "    }\n"
        "    if (!box) return null;\n"
        "    if (offset && Number.isFinite(offset.x) && Number.isFinite(offset.y)) {\n"
        "      return { x: Math.trunc(box.x + offset.x), y: Math.trunc(box.y + offset.y) };\n"
        "    }\n"
        "    return { x: Math.trunc(box.x + box.width / 2), y: Math.trunc(box.y + box.height / 2) };\n"
        "  };\n"
        "  let source = null;\n"
        "  let target = null;\n"
        "  if (params.element_source || params.element_target) {\n"
        "    if (params.element_source) {\n"
        "      source = await getPoint(params.element_source, params.element_source_offset, 'source');\n"
        "      if (!source) {\n"
        "        return { ok: false, error: 'Failed to determine source coordinates from selector. Use the exact visible text (e.g. \"Learn more\" not \"More information\") or a valid CSS/Playwright selector.', source: null, target: null };\n"
        "      }\n"
        "    }\n"
        "    if (params.element_target) {\n"
        "      target = await getPoint(params.element_target, params.element_target_offset, 'target');\n"
        "      if (!target) {\n"
        "        return { ok: false, error: 'Failed to determine target coordinates from selector', source, target: null };\n"
        "      }\n"
        "    }\n"
        "  } else {\n"
        "    const values = [params.coord_source_x, params.coord_source_y, params.coord_target_x, params.coord_target_y];\n"
        "    const allFinite = values.every((v) => Number.isFinite(v));\n"
        "    if (!allFinite) {\n"
        "      return { ok: false, error: 'Must provide either source/target selectors or source/target coordinates' };\n"
        "    }\n"
        "    source = { x: Math.trunc(params.coord_source_x), y: Math.trunc(params.coord_source_y) };\n"
        "    target = { x: Math.trunc(params.coord_target_x), y: Math.trunc(params.coord_target_y) };\n"
        "  }\n"
        "  return { ok: true, source, target, error: null };\n"
        "}"
    )


def _build_drag_script(payload: dict[str, Any]) -> str:
    payload_json = json.dumps(payload, ensure_ascii=False)
    return (
        "async (page) => {\n"
        f"  const params = {payload_json};\n"
        "  if (params.url && String(params.url).trim()) {\n"
        "    await page.goto(String(params.url).trim());\n"
        "  }\n"
        "  const getTextBox = async (query, role) => {\n"
        "    const term = String(query || '').trim().toLowerCase();\n"
        "    if (!term) return null;\n"
        "    return await page.evaluate(({ term, role }) => {\n"
        "      const all = Array.from(document.querySelectorAll('body *'));\n"
        "      const score = (el) => {\n"
        "        const text = String(el.textContent || '').trim().toLowerCase();\n"
        "        if (!text) return -1;\n"
        "        if (text === term) return 2;\n"
        "        if (text.includes(term)) return 1;\n"
        "        return -1;\n"
        "      };\n"
        "      const toVisibleBox = (el) => {\n"
        "        if (!el) return null;\n"
        "        const candidates = role === 'target' && el.parentElement ? [el.parentElement, el] : [el];\n"
        "        for (const candidate of candidates) {\n"
        "          const rect = candidate.getBoundingClientRect();\n"
        "          if (rect && rect.width > 0 && rect.height > 0) {\n"
        "            return { x: rect.x, y: rect.y, width: rect.width, height: rect.height };\n"
        "          }\n"
        "        }\n"
        "        return null;\n"
        "      };\n"
        "      const exactMatches = all.filter((el) => score(el) === 2);\n"
        "      for (const el of exactMatches) {\n"
        "        const box = toVisibleBox(el);\n"
        "        if (box) return box;\n"
        "      }\n"
        "      const fuzzyMatches = all.filter((el) => score(el) === 1);\n"
        "      for (const el of fuzzyMatches) {\n"
        "        const box = toVisibleBox(el);\n"
        "        if (box) return box;\n"
        "      }\n"
        "      return null;\n"
        "    }, { term, role });\n"
        "  };\n"
        "  const extractTextFromHasText = (s) => {\n"
        "    if (!s || typeof s !== 'string') return s;\n"
        "    const m = String(s).match(/:has-text\\s*\\(\\s*['\"]([^'\"]*)['\"]\\s*\\)/);\n"
        "    return m ? m[1] : s;\n"
        "  };\n"
        "  const getPoint = async (selector, offset, role) => {\n"
        "    let box = null;\n"
        "    if (selector) {\n"
        "      try {\n"
        "        const el = await page.$(selector);\n"
        "        if (el) box = await el.boundingBox();\n"
        "      } catch (_err) {\n"
        "        box = null;\n"
        "      }\n"
        "    }\n"
        "    if (!box) {\n"
        "      const textTerm = extractTextFromHasText(selector) || selector;\n"
        "      box = await getTextBox(textTerm, role);\n"
        "    }\n"
        "    if (!box) return null;\n"
        "    if (offset && Number.isFinite(offset.x) && Number.isFinite(offset.y)) {\n"
        "      return { x: Math.trunc(box.x + offset.x), y: Math.trunc(box.y + offset.y) };\n"
        "    }\n"
        "    return { x: Math.trunc(box.x + box.width / 2), y: Math.trunc(box.y + box.height / 2) };\n"
        "  };\n"
        "  let source = null;\n"
        "  let target = null;\n"
        "  if (params.element_source && params.element_target) {\n"
        "    source = await getPoint(params.element_source, params.element_source_offset, 'source');\n"
        "    target = await getPoint(params.element_target, params.element_target_offset, 'target');\n"
        "    if (!source || !target) {\n"
        "      return { ok: false, error: 'Failed to determine source or target coordinates from selectors', source, target };\n"
        "    }\n"
        "  } else {\n"
        "    const values = [params.coord_source_x, params.coord_source_y, params.coord_target_x, params.coord_target_y];\n"
        "    const allFinite = values.every((v) => Number.isFinite(v));\n"
        "    if (!allFinite) {\n"
        "      return { ok: false, error: 'Must provide either source/target selectors or source/target coordinates' };\n"
        "    }\n"
        "    source = { x: Math.trunc(params.coord_source_x), y: Math.trunc(params.coord_source_y) };\n"
        "    target = { x: Math.trunc(params.coord_target_x), y: Math.trunc(params.coord_target_y) };\n"
        "  }\n"
        "  const steps = Math.max(1, Number.isFinite(params.steps) ? Math.trunc(params.steps) : 10);\n"
        "  const delayMs = Math.max(0, Number.isFinite(params.delay_ms) ? Math.trunc(params.delay_ms) : 5);\n"
        "  try {\n"
        "    await page.mouse.move(source.x, source.y);\n"
        "    await page.mouse.down();\n"
        "    for (let i = 1; i <= steps; i += 1) {\n"
        "      const ratio = i / steps;\n"
        "      const x = Math.trunc(source.x + (target.x - source.x) * ratio);\n"
        "      const y = Math.trunc(source.y + (target.y - source.y) * ratio);\n"
        "      await page.mouse.move(x, y);\n"
        "      if (delayMs > 0) {\n"
        "        await new Promise((resolve) => setTimeout(resolve, delayMs));\n"
        "      }\n"
        "    }\n"
        "    await page.mouse.move(target.x, target.y);\n"
        "    await page.mouse.move(target.x, target.y);\n"
        "    await page.mouse.up();\n"
        "  } catch (error) {\n"
        "    return {\n"
        "      ok: false,\n"
        "      error: `Error during drag operation: ${String(error)}`,\n"
        "      source,\n"
        "      target,\n"
        "      steps,\n"
        "      delay_ms: delayMs,\n"
        "    };\n"
        "  }\n"
        "  const message = params.element_source && params.element_target\n"
        "    ? `Dragged element '${params.element_source}' to '${params.element_target}'`\n"
        "    : `Dragged from (${source.x}, ${source.y}) to (${target.x}, ${target.y})`;\n"
        "  return { ok: true, message, source, target, steps, delay_ms: delayMs, error: null };\n"
        "}"
    )


def _build_drag_payload(
    *,
    url: str = "",
    source: str = "",
    target: str = "",
    element_source: str = "",
    element_target: str = "",
    element_source_offset: Any = None,
    element_target_offset: Any = None,
    source_x: Any = None,
    source_y: Any = None,
    target_x: Any = None,
    target_y: Any = None,
    coord_source_x: Any = None,
    coord_source_y: Any = None,
    coord_target_x: Any = None,
    coord_target_y: Any = None,
    steps: Any = None,
    delay_ms: Any = None,
) -> dict[str, Any]:
    source_selector = (element_source or "").strip()
    target_selector = (element_target or "").strip()
    if not source_selector:
        source_selector = (source or "").strip()
    if not target_selector:
        target_selector = (target or "").strip()

    sx = _to_int_or_none(coord_source_x)
    sy = _to_int_or_none(coord_source_y)
    tx = _to_int_or_none(coord_target_x)
    ty = _to_int_or_none(coord_target_y)
    if sx is None:
        sx = _to_int_or_none(source_x)
    if sy is None:
        sy = _to_int_or_none(source_y)
    if tx is None:
        tx = _to_int_or_none(target_x)
    if ty is None:
        ty = _to_int_or_none(target_y)

    return {
        "url": (url or "").strip(),
        "element_source": source_selector,
        "element_target": target_selector,
        "element_source_offset": _normalize_offset(element_source_offset),
        "element_target_offset": _normalize_offset(element_target_offset),
        "coord_source_x": sx,
        "coord_source_y": sy,
        "coord_target_x": tx,
        "coord_target_y": ty,
        "steps": _to_int_or_none(steps),
        "delay_ms": _to_int_or_none(delay_ms),
    }


def _has_selector_inputs(payload: dict[str, Any]) -> bool:
    return bool((payload.get("element_source") or "").strip()) and bool((payload.get("element_target") or "").strip())


def _has_source_selector(payload: dict[str, Any]) -> bool:
    """True if at least element_source is set (for get_element_coordinates, target is optional)."""
    return bool((payload.get("element_source") or "").strip())


def _has_coordinate_inputs(payload: dict[str, Any]) -> bool:
    return all(
        payload.get(k) is not None
        for k in ("coord_source_x", "coord_source_y", "coord_target_x", "coord_target_y")
    )


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


_DEFAULT_CONTROLLER = ActionController(
    actions=_ACTIONS,
    action_specs=_ACTION_SPECS,
    runtime_runner=_RUNTIME_RUNNER,
    lock=_LOCK,
)


def get_default_controller() -> ActionController:
    return _DEFAULT_CONTROLLER


def bind_runtime(runtime: Any) -> None:
    _DEFAULT_CONTROLLER.bind_runtime(runtime)
    global _RUNTIME_RUNNER
    _RUNTIME_RUNNER = _DEFAULT_CONTROLLER._runtime_runner


def bind_runtime_runner(runner: RuntimeRunner | None) -> None:
    _DEFAULT_CONTROLLER.bind_runtime_runner(runner)
    global _RUNTIME_RUNNER
    _RUNTIME_RUNNER = runner


def clear_runtime_runner() -> None:
    bind_runtime_runner(None)


def register_action(name: str, handler: ActionHandler, *, overwrite: bool = True) -> None:
    _DEFAULT_CONTROLLER.register_action(name=name, handler=handler, overwrite=overwrite)


def register_action_spec(
    name: str,
    *,
    summary: str = "",
    when_to_use: str = "",
    params: dict[str, str] | None = None,
) -> None:
    _DEFAULT_CONTROLLER.register_action_spec(
        name=name,
        summary=summary,
        when_to_use=when_to_use,
        params=params,
    )


def list_actions() -> list[str]:
    return _DEFAULT_CONTROLLER.list_actions()


def describe_actions() -> dict[str, dict[str, Any]]:
    return _DEFAULT_CONTROLLER.describe_actions()


async def run_action(
    action: str,
    session_id: str = "",
    request_id: str = "",
    **kwargs: Any,
) -> ActionResult:
    return await _DEFAULT_CONTROLLER.run_action(
        action=action,
        session_id=session_id,
        request_id=request_id,
        **kwargs,
    )


def register_example_actions(controller: ActionController | None = None) -> None:
    ctl = controller or _DEFAULT_CONTROLLER
    async def ping(session_id: str = "", request_id: str = "", **kwargs: Any) -> ActionResult:
        return {
            "ok": True,
            "pong": True,
            "session_id": session_id,
            "request_id": request_id,
            "meta": kwargs,
        }

    async def echo(
        session_id: str = "",
        request_id: str = "",
        text: str = "",
        **kwargs: Any,
    ) -> ActionResult:
        return {
            "ok": True,
            "text": text,
            "session_id": session_id,
            "request_id": request_id,
            "meta": kwargs,
        }

    async def browser_task(
        session_id: str = "",
        request_id: str = "",
        task: str = "",
        timeout_s: int | None = None,
        **kwargs: Any,
    ) -> ActionResult:
        runner = ctl._runtime_runner
        if runner is None:
            return {
                "ok": False,
                "error": "runtime_not_bound: call bind_runtime(...) before browser_task",
                "session_id": session_id,
                "request_id": request_id,
            }

        task_text = (task or "").strip()
        if not task_text:
            return {
                "ok": False,
                "error": "missing required parameter: task",
                "session_id": session_id,
                "request_id": request_id,
            }

        effective_timeout: int | None = None
        if timeout_s is not None:
            try:
                parsed = int(timeout_s)
                if parsed > 0:
                    effective_timeout = parsed
            except Exception:
                effective_timeout = None

        return await runner(
            task=task_text,
            session_id=session_id or None,
            request_id=request_id or None,
            timeout_s=effective_timeout,
        )

    async def browser_get_element_coordinates(
        session_id: str = "",
        request_id: str = "",
        url: str = "",
        source: str = "",
        target: str = "",
        element_source: str = "",
        element_target: str = "",
        element_source_offset: Any = None,
        element_target_offset: Any = None,
        source_x: Any = None,
        source_y: Any = None,
        target_x: Any = None,
        target_y: Any = None,
        coord_source_x: Any = None,
        coord_source_y: Any = None,
        coord_target_x: Any = None,
        coord_target_y: Any = None,
        timeout_s: int | None = None,
        **kwargs: Any,
    ) -> ActionResult:
        del kwargs
        runner = ctl._runtime_runner
        if runner is None:
            return {
                "ok": False,
                "error": "runtime_not_bound: call bind_runtime(...) before browser_get_element_coordinates",
                "session_id": session_id,
                "request_id": request_id,
            }

        payload = _build_drag_payload(
            url=url,
            source=source,
            target=target,
            element_source=element_source,
            element_target=element_target,
            element_source_offset=element_source_offset,
            element_target_offset=element_target_offset,
            source_x=source_x,
            source_y=source_y,
            target_x=target_x,
            target_y=target_y,
            coord_source_x=coord_source_x,
            coord_source_y=coord_source_y,
            coord_target_x=coord_target_x,
            coord_target_y=coord_target_y,
        )
        if not (_has_source_selector(payload) or _has_coordinate_inputs(payload)):
            return {
                "ok": False,
                "error": (
                    "Missing location inputs. Provide at least element_source (element_target is optional), or "
                    "coord_source_x/coord_source_y/coord_target_x/coord_target_y. "
                    "Aliases source/target and source_x/source_y/target_x/target_y are also supported."
                ),
            }
        js_code = _build_coordinate_script(payload)
        task_prompt = _build_run_code_task(js_code, "resolve source/target coordinates")

        runtime_result = await runner(
            task=task_prompt,
            session_id=session_id or None,
            request_id=request_id or None,
            timeout_s=timeout_s if isinstance(timeout_s, int) and timeout_s > 0 else None,
        )
        if not runtime_result.get("ok", False):
            return {
                "ok": False,
                "error": runtime_result.get("error") or "runtime error",
                "runtime": runtime_result,
            }

        parsed = _extract_json_object(runtime_result.get("final"))
        if not parsed:
            final_preview = str(runtime_result.get("final", ""))[:400]
            return {
                "ok": False,
                "error": "Could not parse coordinate result JSON from runtime final output",
                "final_preview": final_preview,
                "runtime": runtime_result,
            }
        return {
            "ok": bool(parsed.get("ok", False)),
            "source": parsed.get("source"),
            "target": parsed.get("target"),
            "error": parsed.get("error"),
            "runtime": runtime_result,
        }

    async def browser_drag_and_drop(
        session_id: str = "",
        request_id: str = "",
        url: str = "",
        source: str = "",
        target: str = "",
        element_source: str = "",
        element_target: str = "",
        element_source_offset: Any = None,
        element_target_offset: Any = None,
        source_x: Any = None,
        source_y: Any = None,
        target_x: Any = None,
        target_y: Any = None,
        coord_source_x: Any = None,
        coord_source_y: Any = None,
        coord_target_x: Any = None,
        coord_target_y: Any = None,
        steps: Any = None,
        delay_ms: Any = None,
        timeout_s: int | None = None,
        **kwargs: Any,
    ) -> ActionResult:
        del kwargs
        runner = ctl._runtime_runner
        if runner is None:
            return {
                "ok": False,
                "error": "runtime_not_bound: call bind_runtime(...) before browser_drag_and_drop",
                "session_id": session_id,
                "request_id": request_id,
            }

        payload = _build_drag_payload(
            url=url,
            source=source,
            target=target,
            element_source=element_source,
            element_target=element_target,
            element_source_offset=element_source_offset,
            element_target_offset=element_target_offset,
            source_x=source_x,
            source_y=source_y,
            target_x=target_x,
            target_y=target_y,
            coord_source_x=coord_source_x,
            coord_source_y=coord_source_y,
            coord_target_x=coord_target_x,
            coord_target_y=coord_target_y,
            steps=steps,
            delay_ms=delay_ms,
        )
        if not (_has_selector_inputs(payload) or _has_coordinate_inputs(payload)):
            return {
                "ok": False,
                "error": (
                    "Missing drag inputs. Provide either "
                    "element_source + element_target, or "
                    "coord_source_x/coord_source_y/coord_target_x/coord_target_y. "
                    "Aliases source/target and source_x/source_y/target_x/target_y are also supported."
                ),
            }
        js_code = _build_drag_script(payload)
        task_prompt = _build_run_code_task(js_code, "drag and drop")

        runtime_result = await runner(
            task=task_prompt,
            session_id=session_id or None,
            request_id=request_id or None,
            timeout_s=timeout_s if isinstance(timeout_s, int) and timeout_s > 0 else None,
        )
        if not runtime_result.get("ok", False):
            return {
                "ok": False,
                "error": runtime_result.get("error") or "runtime error",
                "runtime": runtime_result,
            }

        parsed = _extract_json_object(runtime_result.get("final"))
        if not parsed:
            final_preview = str(runtime_result.get("final", ""))[:400]
            return {
                "ok": False,
                "error": "Could not parse drag result JSON from runtime final output",
                "final_preview": final_preview,
                "runtime": runtime_result,
            }
        return {
            "ok": bool(parsed.get("ok", False)),
            "message": parsed.get("message"),
            "source": parsed.get("source"),
            "target": parsed.get("target"),
            "steps": parsed.get("steps"),
            "delay_ms": parsed.get("delay_ms"),
            "error": parsed.get("error"),
            "runtime": runtime_result,
        }

    ctl.register_action("ping", ping, overwrite=True)
    ctl.register_action_spec(
        "ping",
        summary="Health check action.",
        when_to_use="Use to verify controller dispatch and session/request threading.",
    )
    ctl.register_action("echo", echo, overwrite=True)
    ctl.register_action_spec(
        "echo",
        summary="Echoes provided text and metadata.",
        when_to_use="Use for debugging payload passthrough through browser_custom_action.",
        params={"text": "string: text to echo back"},
    )
    ctl.register_action("browser_task", browser_task, overwrite=True)
    ctl.register_action_spec(
        "browser_task",
        summary="Runs a free-form browser task through runtime.run_browser_task.",
        when_to_use="Use for generic website tasks when no specialized custom action applies.",
        params={
            "task": "string: required task prompt for the browser worker",
            "timeout_s": "int: optional positive timeout override",
        },
    )
    ctl.register_action("run_browser_task", browser_task, overwrite=True)
    ctl.register_action_spec(
        "run_browser_task",
        summary="Alias of browser_task.",
        when_to_use="Same behavior as browser_task.",
        params={
            "task": "string: required task prompt for the browser worker",
            "timeout_s": "int: optional positive timeout override",
        },
    )
    ctl.register_action(
        "browser_get_element_coordinates",
        browser_get_element_coordinates,
        overwrite=True,
    )
    ctl.register_action_spec(
        "browser_get_element_coordinates",
        summary="Resolves source/target screen coordinates by selectors or explicit coordinates.",
        when_to_use=(
            "Use when you need coordinates for one element (element_source only) or two (source + target). "
            "element_target is optional."
        ),
        params={
            "url": "string: optional URL to open before resolving coordinates",
            "element_source": "string: source selector/text alias (required for selector mode)",
            "element_target": "string: target selector/text alias (optional)",
            "coord_source_x": "int: source x coordinate",
            "coord_source_y": "int: source y coordinate",
            "coord_target_x": "int: target x coordinate",
            "coord_target_y": "int: target y coordinate",
            "source/target": "string aliases for element_source/element_target",
            "source_x/source_y/target_x/target_y": "int aliases for coord_* fields",
        },
    )
    ctl.register_action(
        "browser_drag_and_drop",
        browser_drag_and_drop,
        overwrite=True,
    )
    ctl.register_action_spec(
        "browser_drag_and_drop",
        summary="Performs drag-and-drop using selectors or explicit coordinates.",
        when_to_use=(
            "Use for drag-and-drop tasks instead of generic browser_run_task text-only instructions."
        ),
        params={
            "url": "string: optional URL to open before drag-and-drop",
            "element_source": "string: source selector/text alias",
            "element_target": "string: target selector/text alias",
            "coord_source_x": "int: source x coordinate",
            "coord_source_y": "int: source y coordinate",
            "coord_target_x": "int: target x coordinate",
            "coord_target_y": "int: target y coordinate",
            "steps": "int: optional drag interpolation steps",
            "delay_ms": "int: optional delay between drag steps",
            "source/target": "string aliases for element_source/element_target",
            "source_x/source_y/target_x/target_y": "int aliases for coord_* fields",
        },
    )


__all__ = [
    "BaseController",
    "ActionController",
    "get_default_controller",
    "bind_runtime",
    "bind_runtime_runner",
    "clear_runtime_runner",
    "register_action",
    "register_action_spec",
    "register_example_actions",
    "list_actions",
    "describe_actions",
    "run_action",
]
