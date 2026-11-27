"""Policy API routes (static policies only)."""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter(tags=["policies"])

logging.basicConfig(level=logging.INFO)
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
