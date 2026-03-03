"""Episode management routes."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Episode, Shot, Character, ShotType, AssetStatus
from app.services.parser import parse_visual_plan

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


@router.post("/episodes/create")
async def create_episode(
    request: Request,
    number: int = Form(...),
    title: str = Form(...),
    slug: str = Form(""),
    visual_plan: UploadFile = File(None),
    db: AsyncSession = Depends(get_db),
):
    """Create a new episode, optionally with a visual plan file."""
    if not slug:
        slug = f"ep{number:02d}-{title.lower().replace(' ', '-')[:50]}"

    episode = Episode(number=number, title=title, slug=slug)

    if visual_plan and visual_plan.filename:
        content = await visual_plan.read()
        episode.visual_plan_raw = content.decode("utf-8")

    db.add(episode)
    await db.commit()
    await db.refresh(episode)

    # Auto-parse if visual plan was provided
    if episode.visual_plan_raw:
        return RedirectResponse(
            url=f"/episodes/{episode.id}/parse", status_code=303
        )

    return RedirectResponse(url=f"/episodes/{episode.id}", status_code=303)


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

    # Group shots by segment
    segments = {}
    for shot in episode.shots:
        seg = shot.segment or "Unsorted"
        if seg not in segments:
            segments[seg] = []
        segments[seg].append(shot)

    return request.app.state.templates.TemplateResponse(
        "episode.html", {
            "request": request,
            "episode": episode,
            "segments": segments,
            "AssetStatus": AssetStatus,
            "ShotType": ShotType,
        }
    )


@router.get("/episodes/{episode_id}/parse")
async def parse_episode(
    episode_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Parse the visual plan and create shots + characters."""
    result = await db.execute(
        select(Episode)
        .where(Episode.id == episode_id)
        .options(selectinload(Episode.shots), selectinload(Episode.characters))
    )
    episode = result.scalar_one_or_none()
    if not episode:
        raise HTTPException(status_code=404, detail="Episode not found")

    if not episode.visual_plan_raw:
        raise HTTPException(status_code=400, detail="No visual plan to parse")

    parsed = parse_visual_plan(episode.visual_plan_raw)

    if parsed.title:
        episode.title = parsed.title
    if parsed.location:
        episode.location = parsed.location

    # Clear existing shots/characters if re-parsing
    for shot in list(episode.shots):
        await db.delete(shot)
    for char in list(episode.characters):
        if not char.is_main:  # Keep main characters
            await db.delete(char)

    # Create characters
    existing_names = {c.name for c in episode.characters}
    for pc in parsed.characters:
        if pc.name not in existing_names:
            char = Character(
                episode_id=episode.id,
                name=pc.name,
                description=pc.description,
                prompt=pc.prompt,
                is_main=False,
            )
            db.add(char)

    # Create shots
    for ps in parsed.shots:
        shot = Shot(
            episode_id=episode.id,
            number=ps.number,
            name=ps.name,
            segment=ps.segment,
            shot_type=ShotType(ps.shot_type),
            nano_prompt=ps.nano_prompt,
            veo3_prompt=ps.veo3_prompt,
            dialogue=ps.dialogue,
            direction_notes=ps.direction_notes,
            character_refs=ps.character_refs,
            duration=ps.duration,
            camera_notes=ps.camera_notes,
        )
        db.add(shot)

    episode.parsed_at = datetime.now(timezone.utc)
    await db.commit()

    return RedirectResponse(url=f"/episodes/{episode.id}", status_code=303)


@router.post("/episodes/{episode_id}/upload-plan")
async def upload_visual_plan(
    episode_id: int,
    visual_plan: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """Upload or replace a visual plan for an existing episode."""
    result = await db.execute(select(Episode).where(Episode.id == episode_id))
    episode = result.scalar_one_or_none()
    if not episode:
        raise HTTPException(status_code=404, detail="Episode not found")

    content = await visual_plan.read()
    episode.visual_plan_raw = content.decode("utf-8")
    await db.commit()

    return RedirectResponse(url=f"/episodes/{episode.id}/parse", status_code=303)
