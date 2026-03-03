"""
Gorb & Pleck Production Studio
FastAPI application for managing AI-generated mockumentary assets.
"""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import init_db, async_session
from app.api import episodes, generation, callbacks, popups, asset_routes, status_routes
from app.api import settings_routes
from app.services.scheduler import start_scheduler, stop_scheduler
from app.services.scanner import scan_episodes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    logger.info("Starting Gorb & Pleck Studio...")
    await init_db()

    # Load runtime settings from DB
    async with async_session() as db:
        await settings_routes.load_settings_from_db(db)
        logger.info(f"Episodes directory: {settings.effective_asset_dir}")

    # Ensure episode directory exists
    settings.asset_path.mkdir(parents=True, exist_ok=True)

    # Auto-scan episodes on startup
    async with async_session() as db:
        summary = await scan_episodes(db)
        logger.info(
            f"Episode scan: found={summary['found']}, "
            f"created={summary['created']}, parsed={summary['parsed']}"
        )

    api_key = settings.effective_kie_api_key
    if api_key:
        start_scheduler()
        logger.info(f"Background poller started (key: ...{api_key[-6:]})")
    else:
        logger.warning("No KIE_API_KEY set - poller disabled. Set key in Settings page and restart.")

    yield

    stop_scheduler()
    logger.info("Studio shutdown complete")


app = FastAPI(
    title="Gorb & Pleck Studio",
    description="Production asset manager for the mockumentary series",
    lifespan=lifespan,
)

# Static files + templates
static_dir = Path(__file__).parent / "static"
template_dir = Path(__file__).parent / "templates"

app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
# NOTE: Asset serving is handled by a dynamic route in asset_routes.py
# so it always uses the current settings.asset_path (which may change via Settings page).
# app.mount("/assets", ...) was removed because it captured the path at import time,
# before DB settings load.

import json
from urllib.parse import quote as urlquote

templates = Jinja2Templates(directory=str(template_dir))
templates.env.filters["urlencode_path"] = lambda s: '/'.join(urlquote(seg, safe='') for seg in str(s).split('/'))
from markupsafe import Markup
templates.env.filters["tojson"] = lambda v: Markup(json.dumps(v))
app.state.templates = templates

# Routes
app.include_router(episodes.router)
app.include_router(generation.router)
app.include_router(callbacks.router)
app.include_router(settings_routes.router)
app.include_router(popups.router)
app.include_router(asset_routes.router)
app.include_router(status_routes.router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=True,
    )
