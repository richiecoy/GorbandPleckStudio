"""Live status polling endpoint for episode detail page."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Episode, Shot, Character, AssetStatus, ShotType

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
        img_st, vid_st = _derive_statuses(s)
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


def _derive_statuses(shot: Shot) -> tuple[str, str]:
    """
    Derive separate image and video statuses from the single shot status field.

    Returns (image_status, video_status).
    Video status is only meaningful for veo3_clip shots.
    """
    status = shot.status.value
    has_img = bool(shot.image_path)
    has_vid = bool(shot.video_path)
    is_clip = shot.shot_type == ShotType.VEO3_CLIP

    # ── Non-clip shots: only image matters ──
    if not is_clip:
        return (status, "n/a")

    # ── Veo3 clips: two-phase workflow ──
    # Phase 1: image (start frame)
    # Phase 2: video clip

    if not has_img:
        # Still working on image
        return (status, "locked")

    # Have image. Determine image vs video state.
    if not has_vid:
        if status == "review":
            # Image just generated, awaiting approval
            return ("review", "locked")
        elif status == "pending":
            # Image approved, video not started yet
            return ("approved", "pending")
        elif status == "generating":
            # Video is generating
            return ("approved", "generating")
        elif status == "failed":
            # Could be video gen failed (image exists but video doesn't)
            return ("approved", "failed")
        elif status == "approved":
            # Shouldn't happen without video, but be safe
            return ("approved", "pending")
        else:
            return ("approved", status)
    else:
        # Have both image and video
        if status == "review":
            return ("approved", "review")
        elif status == "approved":
            return ("approved", "approved")
        elif status == "failed":
            return ("approved", "failed")
        else:
            return ("approved", status)
