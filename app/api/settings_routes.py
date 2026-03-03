"""Settings management routes."""
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import AppSetting
from app.config import settings

router = APIRouter(prefix="/settings")

# Keys that can be configured via the UI
EDITABLE_KEYS = {
    "kie_api_key": {"label": "Kie.ai API Key", "type": "password",
                    "help": "Bearer token for kie.ai API access"},
    "asset_dir": {"label": "Episodes Directory", "type": "text",
                  "help": "Path to the episodes folder inside the container (e.g. /episodes)"},
    "default_image_model": {"label": "Default Image Model", "type": "text",
                            "help": "nano-banana-pro or nano-banana-2"},
    "default_video_model": {"label": "Default Video Model", "type": "text",
                            "help": "veo3 or veo3_fast"},
    "poll_interval": {"label": "Poll Interval (seconds)", "type": "number",
                      "help": "How often to check kie.ai for completed tasks"},
}


@router.get("/", response_class=HTMLResponse)
async def settings_page(request: Request, db: AsyncSession = Depends(get_db)):
    """Show settings page with current values."""
    result = await db.execute(select(AppSetting))
    db_settings = {s.key: s.value for s in result.scalars().all()}

    # Merge: DB overrides > env defaults
    current = {}
    for key, meta in EDITABLE_KEYS.items():
        db_val = db_settings.get(key)
        if db_val is not None:
            current[key] = db_val
        else:
            current[key] = getattr(settings, key, "")

    return request.app.state.templates.TemplateResponse(
        "settings.html", {
            "request": request,
            "settings_meta": EDITABLE_KEYS,
            "current": current,
            "saved": request.query_params.get("saved", "") == "1",
        }
    )


@router.post("/save")
async def save_settings(request: Request, db: AsyncSession = Depends(get_db)):
    """Save settings to database and update runtime config."""
    form = await request.form()

    for key in EDITABLE_KEYS:
        value = form.get(key, "")
        if isinstance(value, str):
            value = value.strip()

        # Upsert
        result = await db.execute(select(AppSetting).where(AppSetting.key == key))
        existing = result.scalar_one_or_none()
        if existing:
            existing.value = value
        else:
            db.add(AppSetting(key=key, value=value))

        # Update runtime
        settings.set_override(key, value)

    await db.commit()

    return RedirectResponse(url="/settings/?saved=1", status_code=303)


async def load_settings_from_db(db: AsyncSession):
    """Load saved settings from DB into runtime config. Called at startup."""
    result = await db.execute(select(AppSetting))
    for s in result.scalars().all():
        if s.value:  # Only override if non-empty
            settings.set_override(s.key, s.value)
