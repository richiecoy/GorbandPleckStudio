"""Callback handler for kie.ai webhooks and live status polling."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Generation, Shot, Episode, Character, AssetStatus

router = APIRouter()


@router.post("/api/callbacks/kie")
async def kie_callback(request: Request, db: AsyncSession = Depends(get_db)):
    """
    Webhook endpoint for kie.ai to push completed task results.
    This is an alternative to polling — only used if CALLBACK_BASE_URL is set.
    """
    body = await request.json()
    # Kie.ai sends: {"code": 200, "msg": "...", "data": {"taskId": "...", "info": {...}}}
    # For now we just acknowledge — the poller will pick up the result.
    # A more sophisticated version would process inline.
    return JSONResponse({"status": "received"})


@router.get("/api/status/episode/{episode_id}")
async def episode_status(episode_id: int, db: AsyncSession = Depends(get_db)):
    """Return current generation status for all shots in an episode. Used by frontend polling."""
    result = await db.execute(
        select(Episode)
        .where(Episode.id == episode_id)
        .options(
            selectinload(Episode.shots).selectinload(Shot.generations),
            selectinload(Episode.characters).selectinload(Character.generations),
        )
    )
    episode = result.scalar_one_or_none()
    if not episode:
        return JSONResponse({"error": "not found"}, status_code=404)

    shots_data = []
    for shot in episode.shots:
        latest_img = shot.latest_image_gen
        latest_vid = shot.latest_video_gen
        shots_data.append({
            "id": shot.id,
            "number": shot.number,
            "status": shot.status.value,
            "image_status": latest_img.status.value if latest_img else None,
            "video_status": latest_vid.status.value if latest_vid else None,
            "image_path": shot.image_path,
            "video_path": shot.video_path,
        })

    chars_data = []
    for char in episode.characters:
        chars_data.append({
            "id": char.id,
            "name": char.name,
            "status": char.status.value,
            "has_reference": bool(char.reference_image_path),
        })

    return {
        "episode_id": episode_id,
        "stats": episode.stats,
        "shots": shots_data,
        "characters": chars_data,
    }


@router.get("/api/status/shot/{shot_id}")
async def shot_status(shot_id: int, db: AsyncSession = Depends(get_db)):
    """Detailed status for a single shot."""
    result = await db.execute(
        select(Shot).where(Shot.id == shot_id)
        .options(selectinload(Shot.generations))
    )
    shot = result.scalar_one_or_none()
    if not shot:
        return JSONResponse({"error": "not found"}, status_code=404)

    latest_img = shot.latest_image_gen
    latest_vid = shot.latest_video_gen

    return {
        "id": shot.id,
        "number": shot.number,
        "name": shot.name,
        "status": shot.status.value,
        "shot_type": shot.shot_type.value,
        "image": {
            "status": latest_img.status.value if latest_img else "none",
            "path": shot.image_path,
            "url": shot.image_url,
        } if latest_img else None,
        "video": {
            "status": latest_vid.status.value if latest_vid else "none",
            "path": shot.video_path,
            "url": shot.video_url,
        } if latest_vid else None,
    }
