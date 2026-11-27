from __future__ import annotations

from fastapi import APIRouter, FastAPI

from .application_config import application_config_router
from .distributed_service import distributed_service_router
from .physical_resource import physical_resource_router

api_router = APIRouter()


def router_register(app: FastAPI) -> None:
    v1_router = APIRouter(prefix="/v1")
    v1_router.include_router(
        physical_resource_router,
        prefix="",
        tags=["Physical Resource"],
    )
    v1_router.include_router(
        distributed_service_router,
        prefix="",
        tags=["Distributed Service"],
    )
    v1_router.include_router(
        application_config_router,
        prefix="",
        tags=["Application Config"],
    )

    @api_router.get("/health", tags=["System"])
    async def health_check() -> dict[str, str]:
        return {"status": "ok"}

    api_router.include_router(v1_router)
    app.include_router(api_router, prefix="/api")

    @app.get("/", tags=["System"])
    async def root() -> dict[str, str]:
        return {
            "message": "Agent Client REST API",
            "docs": "/docs",
            "health": "/api/health",
        }
