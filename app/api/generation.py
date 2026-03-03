"""Generation management routes - trigger, approve, reject, redo."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import (
    Shot, Character, Generation, Episode,
    AssetStatus, GenerationType, ShotType,
)
from app.services.kie_client import kie

router = APIRouter(prefix="/api/generate")


@router.post("/character/{character_id}")
async def generate_character(
    character_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Generate a reference image for a bystander character."""
    result = await db.execute(
        select(Character).where(Character.id == character_id)
    )
    char = result.scalar_one_or_none()
    if not char:
        raise HTTPException(status_code=404)

    # Build prompt from character description if no explicit prompt
    prompt = char.prompt or char.description
    if not prompt:
        raise HTTPException(status_code=400, detail="Character has no prompt/description")

    # Prepend standard framing for character reference sheets
    full_prompt = (
        f"Photorealistic 3D CGI render, Pixar-quality. "
        f"Character reference portrait, head and upper body, neutral background. "
        f"{prompt}"
    )

    task_result = await kie.generate_image(
        prompt=full_prompt,
        aspect_ratio="1:1",
        resolution="1K",
    )

    if not task_result.success:
        raise HTTPException(status_code=502, detail=task_result.error)

    gen = Generation(
        character_id=char.id,
        gen_type=GenerationType.CHARACTER,
        status=AssetStatus.GENERATING,
        task_id=task_result.task_id,
        prompt_used=full_prompt,
    )
    db.add(gen)

    char.status = AssetStatus.GENERATING
    await db.commit()

    return {"ok": True, "task_id": task_result.task_id}


@router.post("/shot/{shot_id}/image")
async def generate_shot_image(
    shot_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Generate a Nano Banana image (still or start frame) for a shot."""
    result = await db.execute(
        select(Shot)
        .where(Shot.id == shot_id)
        .options(selectinload(Shot.episode).selectinload(Episode.characters))
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404)

    if not shot.nano_prompt:
        raise HTTPException(status_code=400, detail="Shot has no Nano Banana prompt")

    # Gather character reference URLs
    ref_urls = _get_reference_urls(shot)

    gen_type = (
        GenerationType.START_FRAME
        if shot.shot_type == ShotType.VEO3_CLIP
        else GenerationType.STILL
    )

    task_result = await kie.generate_image(
        prompt=shot.nano_prompt,
        reference_urls=ref_urls if ref_urls else None,
    )

    if not task_result.success:
        raise HTTPException(status_code=502, detail=task_result.error)

    gen = Generation(
        shot_id=shot.id,
        gen_type=gen_type,
        status=AssetStatus.GENERATING,
        task_id=task_result.task_id,
        prompt_used=shot.nano_prompt,
        reference_urls=ref_urls,
    )
    db.add(gen)

    shot.status = AssetStatus.GENERATING
    await db.commit()

    return {"ok": True, "task_id": task_result.task_id}


@router.post("/shot/{shot_id}/video")
async def generate_shot_video(
    shot_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Generate a Veo 3 video clip from an approved start frame."""
    result = await db.execute(
        select(Shot)
        .where(Shot.id == shot_id)
        .options(selectinload(Shot.generations))
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404)

    if shot.shot_type != ShotType.VEO3_CLIP:
        raise HTTPException(status_code=400, detail="Shot is not a Veo3 clip type")

    if not shot.veo3_prompt:
        raise HTTPException(status_code=400, detail="Shot has no Veo3 prompt")

    # Need an approved start frame image
    start_frame_url = shot.image_url
    if not start_frame_url:
        raise HTTPException(
            status_code=400,
            detail="No start frame image. Generate and approve an image first."
        )

    task_result = await kie.generate_video(
        prompt=shot.veo3_prompt,
        image_urls=[start_frame_url],
    )

    if not task_result.success:
        raise HTTPException(status_code=502, detail=task_result.error)

    gen = Generation(
        shot_id=shot.id,
        gen_type=GenerationType.VIDEO,
        status=AssetStatus.GENERATING,
        task_id=task_result.task_id,
        prompt_used=shot.veo3_prompt,
        reference_urls=[start_frame_url],
    )
    db.add(gen)

    shot.status = AssetStatus.GENERATING
    await db.commit()

    return {"ok": True, "task_id": task_result.task_id}


@router.post("/shot/{shot_id}/approve-image")
async def approve_shot_image(shot_id: int, db: AsyncSession = Depends(get_db)):
    """Approve the current image for a shot."""
    shot = await _get_shot(shot_id, db)
    latest = shot.latest_image_gen
    if not latest or latest.status != AssetStatus.REVIEW:
        raise HTTPException(status_code=400, detail="No image in review")

    latest.status = AssetStatus.APPROVED

    # If this is a veo3 shot, image approval means start frame is ready
    # but overall shot status stays at review until video is also done
    if shot.shot_type == ShotType.VEO3_CLIP:
        # Start frame approved, shot awaits video generation
        shot.status = AssetStatus.PENDING  # Ready for video gen
    else:
        shot.status = AssetStatus.APPROVED

    await db.commit()
    return {"ok": True}


@router.post("/shot/{shot_id}/approve-video")
async def approve_shot_video(shot_id: int, db: AsyncSession = Depends(get_db)):
    """Approve the current video for a shot."""
    shot = await _get_shot(shot_id, db)
    latest = shot.latest_video_gen
    if not latest or latest.status != AssetStatus.REVIEW:
        raise HTTPException(status_code=400, detail="No video in review")

    latest.status = AssetStatus.APPROVED
    shot.status = AssetStatus.APPROVED
    await db.commit()
    return {"ok": True}


@router.post("/shot/{shot_id}/reject-image")
async def reject_shot_image(shot_id: int, db: AsyncSession = Depends(get_db)):
    """Reject the current image — ready for redo."""
    shot = await _get_shot(shot_id, db)
    latest = shot.latest_image_gen
    if latest:
        latest.status = AssetStatus.REJECTED
    shot.status = AssetStatus.PENDING
    shot.image_path = None
    shot.image_url = None
    await db.commit()
    return {"ok": True}


@router.post("/shot/{shot_id}/reject-video")
async def reject_shot_video(shot_id: int, db: AsyncSession = Depends(get_db)):
    """Reject the current video — ready for redo."""
    shot = await _get_shot(shot_id, db)
    latest = shot.latest_video_gen
    if latest:
        latest.status = AssetStatus.REJECTED
    shot.status = AssetStatus.PENDING
    shot.video_path = None
    shot.video_url = None
    await db.commit()
    return {"ok": True}


@router.post("/character/{character_id}/approve")
async def approve_character(character_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Character).where(Character.id == character_id)
        .options(selectinload(Character.generations))
    )
    char = result.scalar_one_or_none()
    if not char:
        raise HTTPException(status_code=404)

    for gen in char.generations:
        if gen.status == AssetStatus.REVIEW:
            gen.status = AssetStatus.APPROVED
            break

    char.status = AssetStatus.APPROVED

    # Upload approved reference to kie.ai for use in shot generation
    if char.reference_image_path and not char.reference_image_url:
        url = await kie.upload_file(char.reference_image_path)
        if url:
            char.reference_image_url = url

    await db.commit()
    return {"ok": True}


@router.post("/character/{character_id}/reject")
async def reject_character(character_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Character).where(Character.id == character_id)
        .options(selectinload(Character.generations))
    )
    char = result.scalar_one_or_none()
    if not char:
        raise HTTPException(status_code=404)

    for gen in char.generations:
        if gen.status == AssetStatus.REVIEW:
            gen.status = AssetStatus.REJECTED
            break

    char.status = AssetStatus.PENDING
    char.reference_image_path = None
    char.reference_image_url = None
    await db.commit()
    return {"ok": True}


# ── Batch Operations ─────────────────────────────────────────────────

@router.post("/episode/{episode_id}/generate-all-characters")
async def generate_all_characters(episode_id: int, db: AsyncSession = Depends(get_db)):
    """Generate reference images for all pending characters in an episode."""
    result = await db.execute(
        select(Character)
        .where(Character.episode_id == episode_id)
        .where(Character.status == AssetStatus.PENDING)
        .where(Character.is_main == False)
    )
    chars = result.scalars().all()
    tasks = []

    for char in chars:
        prompt = char.prompt or char.description
        if not prompt:
            continue

        full_prompt = (
            f"Photorealistic 3D CGI render, Pixar-quality. "
            f"Character reference portrait, head and upper body, neutral background. "
            f"{prompt}"
        )

        task_result = await kie.generate_image(
            prompt=full_prompt,
            aspect_ratio="1:1",
            resolution="1K",
        )

        if task_result.success:
            gen = Generation(
                character_id=char.id,
                gen_type=GenerationType.CHARACTER,
                status=AssetStatus.GENERATING,
                task_id=task_result.task_id,
                prompt_used=full_prompt,
            )
            db.add(gen)
            char.status = AssetStatus.GENERATING
            tasks.append(task_result.task_id)

    await db.commit()
    return {"ok": True, "tasks_started": len(tasks)}


@router.post("/episode/{episode_id}/generate-all-images")
async def generate_all_images(episode_id: int, db: AsyncSession = Depends(get_db)):
    """Generate images for all pending shots that need them."""
    result = await db.execute(
        select(Shot)
        .where(Shot.episode_id == episode_id)
        .where(Shot.status == AssetStatus.PENDING)
        .where(Shot.shot_type.in_([ShotType.STILL, ShotType.VEO3_CLIP, ShotType.TITLE_CARD]))
        .options(selectinload(Shot.episode).selectinload(Episode.characters))
    )
    shots = result.scalars().all()
    tasks = []

    for shot in shots:
        if not shot.nano_prompt:
            continue

        ref_urls = _get_reference_urls(shot)
        gen_type = (
            GenerationType.START_FRAME
            if shot.shot_type == ShotType.VEO3_CLIP
            else GenerationType.STILL
        )

        task_result = await kie.generate_image(
            prompt=shot.nano_prompt,
            reference_urls=ref_urls if ref_urls else None,
        )

        if task_result.success:
            gen = Generation(
                shot_id=shot.id,
                gen_type=gen_type,
                status=AssetStatus.GENERATING,
                task_id=task_result.task_id,
                prompt_used=shot.nano_prompt,
                reference_urls=ref_urls,
            )
            db.add(gen)
            shot.status = AssetStatus.GENERATING
            tasks.append(task_result.task_id)

    await db.commit()
    return {"ok": True, "tasks_started": len(tasks)}


# ── Helpers ──────────────────────────────────────────────────────────

async def _get_shot(shot_id: int, db: AsyncSession) -> Shot:
    result = await db.execute(
        select(Shot).where(Shot.id == shot_id)
        .options(selectinload(Shot.generations))
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404)
    return shot


def _get_reference_urls(shot: Shot) -> list[str]:
    """Gather approved character reference URLs for a shot."""
    urls = []
    if not shot.episode or not shot.episode.characters:
        return urls

    char_map = {c.name.lower(): c for c in shot.episode.characters}

    for ref_name in (shot.character_refs or []):
        char = char_map.get(ref_name.lower())
        if char and char.reference_image_url and char.status == AssetStatus.APPROVED:
            urls.append(char.reference_image_url)

    return urls
