from __future__ import annotations

from fastapi import FastAPI

from .routers.register import router_register


def create_app() -> FastAPI:
    app = FastAPI(
        title="Agent Client REST API",
        version="0.1.0",
        description="REST API scaffold for agent client extension.",
    )
    router_register(app)
    return app
