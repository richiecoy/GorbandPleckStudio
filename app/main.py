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
from app.api import episodes, generation, callbacks
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

    if settings.effective_kie_api_key:
        start_scheduler()
        logger.info("Background poller started")
    else:
        logger.warning("No KIE_API_KEY set - generation disabled")

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
app.mount("/assets", StaticFiles(directory=str(settings.asset_path)), name="assets")

from urllib.parse import quote as urlquote

templates = Jinja2Templates(directory=str(template_dir))
templates.env.filters["urlencode_path"] = lambda s: urlquote(str(s), safe="/")
app.state.templates = templates

# Routes
app.include_router(episodes.router)
app.include_router(generation.router)
app.include_router(callbacks.router)
app.include_router(settings_routes.router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=True,
    )
