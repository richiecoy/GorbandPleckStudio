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
from app.database import init_db
from app.api import episodes, generation, callbacks
from app.services.scheduler import start_scheduler, stop_scheduler

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

    # Ensure asset directories exist
    settings.asset_path.mkdir(parents=True, exist_ok=True)
    (settings.asset_path / "characters").mkdir(exist_ok=True)
    (settings.asset_path / "episodes").mkdir(exist_ok=True)

    if settings.kie_api_key:
        start_scheduler()
        logger.info("Background poller started")
    else:
        logger.warning("No KIE_API_KEY set — generation disabled")

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

# Also serve generated assets for preview
app.mount("/assets", StaticFiles(directory=str(settings.asset_path)), name="assets")

templates = Jinja2Templates(directory=str(template_dir))
app.state.templates = templates

# Routes
app.include_router(episodes.router)
app.include_router(generation.router)
app.include_router(callbacks.router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=True,
    )
