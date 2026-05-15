"""ATLAS FastAPI application entry point.

Composes:
    - Logging
    - Database (schema + seed)
    - Agent coordinator (5 agents as background tasks)
    - HTTP API + WebSocket routes
    - Static dashboard files
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from atlas import __version__
from atlas.agents.coordinator import get_coordinator
from atlas.api.routes import api_router
from atlas.api.ws import websocket_router
from atlas.config import get_settings
from atlas.db.seed import initialise_database
from atlas.logging_setup import get_logger, setup_logging


BANNER = r"""
   _  _____ _      _   ___
  / \|_   _| |    / \ / __|
 / _ \ | | | |   / _ \\__ \
/_/ \_\|_| |_|_ /_/ \_\___/
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(level=settings.log_level, log_dir=settings.logs_dir,
                  to_file=settings.log_to_file)
    log = get_logger("server")

    log.info(BANNER)
    log.info("ATLAS %s starting", __version__)
    log.info("Install root: %s", settings.install_root)
    log.info("Database:     %s", settings.database_url)
    log.info("Server:       http://%s:%d", settings.server_host, settings.server_port)
    if settings.simulation_mode:
        log.warning("SIMULATION MODE — no real hardware will be commanded")

    # Database
    initialise_database()

    # Agents
    coord = get_coordinator()
    await coord.start_all()
    log.info("ATLAS is ready.")

    try:
        yield
    finally:
        log.info("ATLAS shutting down...")
        await coord.stop_all()
        log.info("Goodbye.")


app = FastAPI(
    title="ATLAS",
    description="Autonomous Telescope & Learning Astronomy System",
    version=__version__,
    lifespan=lifespan,
)

# API routes
app.include_router(api_router)
app.include_router(websocket_router)

# Static dashboard
_dashboard_root = get_settings().dashboard_dir
if _dashboard_root.exists():
    app.mount("/static", StaticFiles(directory=str(_dashboard_root)), name="static")


@app.get("/", include_in_schema=False)
async def root():
    """Serve the dashboard index.html."""
    index = _dashboard_root / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return JSONResponse({
        "name": "ATLAS",
        "version": __version__,
        "status": "running",
        "note": "Dashboard not installed. API is at /api.",
    })


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    p = _dashboard_root / "assets" / "favicon.ico"
    if p.exists():
        return FileResponse(str(p))
    return JSONResponse({}, status_code=204)
