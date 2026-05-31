"""FastAPI 应用入口。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from finance_agent.api.routes import router


def create_app() -> FastAPI:
    """创建 Dashboard API 应用。"""

    app = FastAPI(
        title="Finance Agent Dashboard API",
        version="0.1.0",
        description="私人金融助手 Web 控制台 API。",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router, prefix="/api")
    return app


app = create_app()
