# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Policy API routes (static policies only)."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from jiuwenbox.logging_config import configure_logging
from jiuwenbox.models.policy import TimeoutPolicy

router = APIRouter(tags=["policies"])

configure_logging()
logger = logging.getLogger(__name__)


def _mgr():
    from jiuwenbox.server.app import get_manager
    return get_manager()


@router.get("/policies/{sandbox_id}")
async def get_policy(sandbox_id: str):
    """Get the policy currently applied to a sandbox."""
    mgr = _mgr()
    policy = await mgr.get_policy(sandbox_id)
    if policy is None:
        return JSONResponse(
            status_code=404,
            content={"error": f"No policy found for sandbox '{sandbox_id}'"},
        )
    return policy.model_dump(mode="json")


class UpdateTimeoutRequest(BaseModel):
    """Partial update payload for ``PUT /timeout``.

    Both fields are optional; only those explicitly present in the JSON body
    are applied (detected via :attr:`pydantic.BaseModel.model_fields_set`).
    This lets a client flip just one knob -- e.g. ``{"idle_timeout": null}``
    to disable reaping without resetting ``idle_check_interval`` -- instead
    of having to round-trip the full state.

    Semantics:

    - ``idle_timeout`` accepts a positive number of seconds, or ``null`` /
      ``<= 0`` to disable reaping. Same normalization as
      :class:`TimeoutPolicy.idle_timeout`.
    - ``idle_check_interval`` accepts a positive number of seconds. Passing
      ``null`` here is rejected (use the GET endpoint to learn the current
      value, or restart the server to fall back to the YAML default).
    """

    idle_timeout: float | None = Field(default=None)
    idle_check_interval: float | None = Field(default=None)


@router.get("/timeout")
async def get_timeout():
    """Return the server-level idle-sandbox reaping configuration.

    Reflects ``mgr.policy.timeout`` -- i.e. the values currently driving the
    background reaper (after any ``PUT /timeout`` updates). When
    ``idle_timeout`` is ``null``, the reaper is disabled.
    """
    mgr = _mgr()
    return mgr.policy.timeout.model_dump(mode="json")


@router.put("/timeout")
async def update_timeout(request: UpdateTimeoutRequest):
    """Update one or both timeout fields and atomically restart the reaper.

    Only fields *explicitly* present in the request body are touched; omitted
    fields preserve their current value. Validation failures (e.g.
    ``idle_check_interval <= 0``) surface as HTTP 400 with the pydantic
    message. Returns the resulting (post-update) :class:`TimeoutPolicy`.
    """
    mgr = _mgr()
    updates = request.model_dump(exclude_unset=True)

    # ``idle_check_interval: null`` is ambiguous -- "unset" already maps to
    # "don't touch this field", so an explicit null here would mean
    # "interval is None", which TimeoutPolicy rejects anyway. Catch it early
    # with a clear message instead of leaking a generic pydantic error.
    if "idle_check_interval" in updates and updates["idle_check_interval"] is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "idle_check_interval cannot be null; omit the field to "
                "preserve the current value, or pass a positive number"
            ),
        )

    current = mgr.policy.timeout.model_dump()
    current.update(updates)
    try:
        new_timeout = TimeoutPolicy.model_validate(current)
    except ValidationError as exc:
        # Surface as 400 instead of bubbling up to the generic exception
        # handler (which would turn it into a 500 here because the handler
        # only catches ``ValidationError`` raised by request body parsing,
        # not by manual ``model_validate`` calls inside route handlers).
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    applied = await mgr.update_timeout_policy(new_timeout)
    logger.info(
        "timeout policy updated: idle_timeout=%s idle_check_interval=%s",
        applied.idle_timeout,
        applied.idle_check_interval,
    )
    return applied.model_dump(mode="json")
