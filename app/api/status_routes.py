"""Live status polling endpoint for episode detail page."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Episode, Shot, Character, AssetStatus, ShotType, _derive_asset_statuses

router = APIRouter(prefix="/api/status")


@router.get("/episode/{episode_id}")
async def episode_status(episode_id: int, db: AsyncSession = Depends(get_db)):
    """
    Returns current status for all shots and characters in an episode.
    Called by the frontend poller every few seconds.
    """
    result = await db.execute(
        select(Episode)
        .where(Episode.id == episode_id)
        .options(
            selectinload(Episode.shots),
            selectinload(Episode.characters),
        )
    )
    episode = result.scalar_one_or_none()
    if not episode:
        raise HTTPException(status_code=404)

    stats = episode.stats

    shots = []
    for s in episode.shots:
        img_st, vid_st = _derive_asset_statuses(s)
        shots.append({
            "id": s.id,
            "status": s.status.value,
            "shot_type": s.shot_type.value,
            "image_status": img_st,
            "video_status": vid_st,
            "has_image": bool(s.image_path),
            "has_video": bool(s.video_path),
            "image_path": s.image_path,
            "video_path": s.video_path,
        })

    characters = [
        {"id": c.id, "status": c.status.value, "has_image": bool(c.reference_image_path)}
        for c in episode.characters
    ]

    return {
        "stats": stats,
        "shots": shots,
        "characters": characters,
    }

