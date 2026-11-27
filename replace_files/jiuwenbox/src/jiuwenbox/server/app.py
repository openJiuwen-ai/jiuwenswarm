"""FastAPI application for box-server."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from jiuwenbox import __version__
from jiuwenbox.server.sandbox_manager import (
    SandboxManager,
    SandboxNotFoundError,
    SandboxStateError,
)
from jiuwenbox.server.policy_engine import PolicyValidationError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_manager: SandboxManager | None = None


def get_manager() -> SandboxManager:
    """Get the global SandboxManager instance."""
    global _manager
    if _manager is None:
        _manager = SandboxManager()
    return _manager


@asynccontextmanager
async def lifespan(_application: FastAPI):
    """Application lifespan: initialize manager on startup."""
    global _manager
    _manager = SandboxManager()
    logger.info("box-server started (version %s)", __version__)
    yield
    logger.info("box-server shutting down")


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    application = FastAPI(
        title="jiuwenbox",
        description="Agent sandbox management API",
        version=__version__,
        lifespan=lifespan,
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Exception handlers
    @application.exception_handler(SandboxNotFoundError)
    async def not_found_handler(request: Request, exc: SandboxNotFoundError):
        return JSONResponse(status_code=404, content={"error": str(exc)})

    @application.exception_handler(SandboxStateError)
    async def state_error_handler(request: Request, exc: SandboxStateError):
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @application.exception_handler(PolicyValidationError)
    async def policy_validation_error_handler(request: Request, exc: PolicyValidationError):
        return JSONResponse(status_code=400, content={"error": str(exc)})

    # Register routes
    from jiuwenbox.server.routes.sandbox import router as sandbox_router
    from jiuwenbox.server.routes.policy import router as policy_router

    application.include_router(sandbox_router, prefix="/api/v1")
    application.include_router(policy_router, prefix="/api/v1")

    @application.get("/health")
    async def health():
        from jiuwenbox.models.common import HealthResponse
        from jiuwenbox.supervisor.landlock import detect_landlock_abi

        mgr = get_manager()
        sandboxes = await mgr.list_sandboxes()
        active = sum(1 for s in sandboxes if s.phase.value == "ready")

        return HealthResponse(
            version=__version__,
            landlock_supported=detect_landlock_abi() > 0,
            sandboxes_active=active,
        )

    return application


app = create_app()
