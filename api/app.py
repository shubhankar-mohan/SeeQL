"""FastAPI application factory for SeeQL."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from api.routes import router
from api.dashboard_routes import router as dashboard_router
from api.dashboard_api import router as dashboard_api_router

BASE_DIR = Path(__file__).resolve().parent.parent


def create_app() -> FastAPI:
    app = FastAPI(
        title="SeeQL",
        description="MySQL DBA Agent - LLM-powered MySQL monitoring",
        version="0.1.0",
    )

    # Static files
    static_dir = BASE_DIR / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # Jinja2 templates
    templates_dir = BASE_DIR / "templates"
    templates = Jinja2Templates(directory=str(templates_dir))
    app.state.templates = templates

    # Core routers
    app.include_router(router)
    app.include_router(dashboard_router)
    app.include_router(dashboard_api_router)

    # Prometheus metrics endpoint
    try:
        from api.prometheus import router as prom_router
        app.include_router(prom_router)
    except ImportError:
        pass  # prometheus_client not installed

    # Agent API routes
    from api.agent_routes import router as agent_router
    app.include_router(agent_router)

    return app


# Module-level instance for uvicorn (uvicorn api.app:app)
app = create_app()
