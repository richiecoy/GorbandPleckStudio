"""
Dynamic asset file serving.

Replaces the static StaticFiles mount so that assets are always served
from the current settings.asset_path (which can change at runtime via
the Settings page). The old static mount captured the path at import
time, before DB settings loaded.
"""
import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.config import settings

router = APIRouter()

# Pre-populate common types
MIME_MAP = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".md": "text/markdown",
}


@router.get("/assets/{file_path:path}")
async def serve_asset(file_path: str):
    """Serve a file from the episodes/assets directory."""
    full_path = settings.asset_path / file_path

    # Security: prevent path traversal
    try:
        full_path.resolve().relative_to(settings.asset_path.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied")

    if not full_path.is_file():
        raise HTTPException(status_code=404, detail=f"Not found: {file_path}")

    suffix = full_path.suffix.lower()
    media_type = MIME_MAP.get(suffix) or mimetypes.guess_type(str(full_path))[0] or "application/octet-stream"

    return FileResponse(
        path=str(full_path),
        media_type=media_type,
        filename=full_path.name,
    )
