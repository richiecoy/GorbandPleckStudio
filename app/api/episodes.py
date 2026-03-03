"""Episode management routes - filesystem scanning."""
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Episode, Shot, Character, ShotType, AssetStatus
from app.services.scanner import scan_episodes, _read_visual_plan, _auto_parse, _link_existing_assets

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    """Main dashboard showing all episodes."""
    result = await db.execute(
        select(Episode).options(selectinload(Episode.shots)).order_by(Episode.number)
    )
    episodes = result.scalars().all()
    return request.app.state.templates.TemplateResponse(
        "dashboard.html", {"request": request, "episodes": episodes}
    )


@router.post("/episodes/scan")
async def scan(request: Request, db: AsyncSession = Depends(get_db)):
    """Scan the episodes directory and sync with database."""
    await scan_episodes(db)
    return RedirectResponse(url="/", status_code=303)


@router.get("/api/episodes/scan")
async def scan_api(db: AsyncSession = Depends(get_db)):
    """Scan endpoint returning JSON summary."""
    summary = await scan_episodes(db)
    return summary


@router.get("/episodes/{episode_id}", response_class=HTMLResponse)
async def episode_detail(
    request: Request,
    episode_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Episode detail page with shot grid and character panel."""
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
        raise HTTPException(status_code=404, detail="Episode not found")

    segments = {}
    for shot in episode.shots:
        seg = shot.segment or "Unsorted"
        if seg not in segments:
            segments[seg] = []
        segments[seg].append(shot)

    # Compute initial image/video statuses and paths for each shot
    from app.models import _derive_asset_statuses
    shot_statuses = {}
    for shot in episode.shots:
        img_st, vid_st = _derive_asset_statuses(shot)
        shot_statuses[shot.id] = {
            "image_status": img_st,
            "video_status": vid_st,
            "image_path": shot.image_path,
            "video_path": shot.video_path,
        }

    # Compute initial character statuses
    char_statuses = {}
    for char in episode.characters:
        char_statuses[char.id] = {
            "status": char.status.value,
            "image_path": char.reference_image_path or "",
            "is_main": char.is_main,
        }

    return request.app.state.templates.TemplateResponse(
        "episode.html", {
            "request": request,
            "episode": episode,
            "segments": segments,
            "shot_statuses": shot_statuses,
            "char_statuses": char_statuses,
            "AssetStatus": AssetStatus,
            "ShotType": ShotType,
        }
    )


@router.post("/episodes/{episode_id}/rescan")
async def rescan_episode(
    episode_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Re-read visual-plan.md from disk and re-parse this episode."""
    from app.config import settings

    result = await db.execute(
        select(Episode)
        .where(Episode.id == episode_id)
        .options(selectinload(Episode.shots), selectinload(Episode.characters))
    )
    episode = result.scalar_one_or_none()
    if not episode:
        raise HTTPException(status_code=404, detail="Episode not found")

    ep_dir = settings.asset_path / episode.slug
    if not ep_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Folder not found: {episode.slug}")

    content = _read_visual_plan(ep_dir)
    if not content:
        raise HTTPException(status_code=404, detail="No visual-plan.md found in folder")

    episode.visual_plan_raw = content
    episode.parsed_at = None

    await _auto_parse(episode, db)
    await db.commit()

    # Re-link existing assets after parse creates new shot/char records
    await _link_existing_assets(episode_id, db)
    await db.commit()

    return RedirectResponse(url=f"/episodes/{episode_id}", status_code=303)
