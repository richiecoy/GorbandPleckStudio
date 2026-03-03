"""Live status polling endpoint for episode detail page."""
import logging
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import (
    Episode, Shot, Character, Generation,
    AssetStatus, ShotType, GenerationType, _derive_asset_statuses,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/status")


def _compute_asset_statuses(shot: Shot) -> tuple[str, str]:
    """Compute per-asset statuses using Generation records when available,
    falling back to _derive_asset_statuses if generations aren't loaded."""

    # Check if generations were eagerly loaded by testing the attribute
    try:
        gens = shot.generations
    except Exception:
        # Generations not loaded — fall back to the original derive function
        return _derive_asset_statuses(shot)

    # If no generation records exist, fall back to derive function
    if not gens:
        return _derive_asset_statuses(shot)

    is_clip = shot.shot_type == ShotType.VEO3_CLIP

    # --- image status from latest generation record ---
    latest_img = shot.latest_image_gen
    if latest_img:
        if latest_img.status == AssetStatus.GENERATING:
            img_st = "generating"
        elif latest_img.status == AssetStatus.REVIEW:
            img_st = "review"
        elif latest_img.status == AssetStatus.APPROVED:
            img_st = "approved"
        elif latest_img.status == AssetStatus.FAILED:
            # If the file exists on disk, treat as approved (old image still valid)
            img_st = "approved" if shot.image_path else "failed"
        else:  # PENDING, REJECTED
            img_st = "approved" if shot.image_path else "pending"
    elif shot.image_path:
        img_st = "approved"
    else:
        img_st = "pending"

    # --- video status ---
    if not is_clip:
        return img_st, "n/a"

    # Video is locked until there is an image file on disk
    if not shot.image_path:
        return img_st, "locked"

    latest_vid = shot.latest_video_gen
    if latest_vid:
        if latest_vid.status == AssetStatus.GENERATING:
            vid_st = "generating"
        elif latest_vid.status == AssetStatus.REVIEW:
            vid_st = "review"
        elif latest_vid.status == AssetStatus.APPROVED:
            vid_st = "approved"
        elif latest_vid.status == AssetStatus.FAILED:
            vid_st = "failed"
        else:
            vid_st = "pending"
    elif shot.video_path:
        vid_st = "approved"
    else:
        vid_st = "pending"

    return img_st, vid_st


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
            selectinload(Episode.shots).selectinload(Shot.generations),
            selectinload(Episode.characters),
        )
    )
    episode = result.scalar_one_or_none()
    if not episode:
        raise HTTPException(status_code=404)

    stats = episode.stats

    shots = []
    for s in episode.shots:
        img_st, vid_st = _compute_asset_statuses(s)
        latest_img = s.latest_image_gen
        gen_count = len(s.generations) if s.generations else 0
        if s.status == AssetStatus.GENERATING or gen_count > 0:
            logger.info(
                f"Shot {s.id} (#{s.number}): status={s.status.value}, "
                f"gens={gen_count}, latest_img_gen={latest_img.status.value if latest_img else None}, "
                f"=> img_st={img_st}, vid_st={vid_st}"
            )
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

    characters = []
    for c in episode.characters:
        logger.info(
            f"POLL char {c.id} ({c.name}): ref_img_path={c.reference_image_path!r}, "
            f"status={c.status.value}"
        )
        characters.append({
            "id": c.id,
            "status": c.status.value,
            "has_image": bool(c.reference_image_path),
            "image_path": c.reference_image_path,
            "is_main": c.is_main,
        })

    return {
        "_api_version": "v9-2026-03-03",
        "stats": stats,
        "shots": shots,
        "characters": characters,
    }


@router.get("/episode/{episode_id}/debug")
async def episode_debug(episode_id: int, db: AsyncSession = Depends(get_db)):
    """Debug endpoint: dump shot inventory to diagnose stats mismatch."""
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

    from collections import Counter
    type_counts = Counter()
    shots_detail = []
    for s in episode.shots:
        type_counts[s.shot_type.value] += 1
        img_st, vid_st = _derive_asset_statuses(s)
        shots_detail.append({
            "id": s.id,
            "number": s.number,
            "name": s.name,
            "segment": s.segment,
            "shot_type": s.shot_type.value,
            "status": s.status.value,
            "image_status": img_st,
            "video_status": vid_st,
            "has_image": bool(s.image_path),
            "has_video": bool(s.video_path),
        })

    chars_detail = []
    for c in episode.characters:
        chars_detail.append({
            "id": c.id,
            "name": c.name,
            "is_main": c.is_main,
            "status": c.status.value,
            "has_image": bool(c.reference_image_path),
        })

    # Compute expected total
    gen_types = {"still", "veo3_clip", "title_card"}
    img_count = sum(1 for s in episode.shots if s.shot_type.value in gen_types)
    vid_count = sum(1 for s in episode.shots if s.shot_type.value == "veo3_clip")
    char_count = sum(1 for c in episode.characters if not c.is_main)

    return {
        "total_shots_in_db": len(episode.shots),
        "total_characters_in_db": len(episode.characters),
        "type_counts": dict(type_counts),
        "expected_stats": {
            "images": img_count,
            "videos": vid_count,
            "characters": char_count,
            "total": img_count + vid_count + char_count,
        },
        "shots": shots_detail,
        "characters": chars_detail,
    }

