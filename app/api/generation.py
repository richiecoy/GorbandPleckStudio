"""Generation management routes - trigger, approve, reject, redo, import, preview."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Body
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import (
    Shot, Character, Generation, Episode,
    AssetStatus, GenerationType, ShotType,
)
from app.config import settings
from app.services.kie_client import kie

router = APIRouter(prefix="/api/generate")


# ── Character Generation ────────────────────────────────────────────

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

    prompt = char.prompt or char.description
    if not prompt:
        raise HTTPException(status_code=400, detail="Character has no prompt/description")

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


# ── Shot Image Generation ───────────────────────────────────────────

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


# ── Shot Video Generation ───────────────────────────────────────────

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


# ── Approve / Reject ────────────────────────────────────────────────

@router.post("/shot/{shot_id}/approve-image")
async def approve_shot_image(shot_id: int, db: AsyncSession = Depends(get_db)):
    shot = await _get_shot(shot_id, db)
    latest = shot.latest_image_gen
    if not latest or latest.status != AssetStatus.REVIEW:
        raise HTTPException(status_code=400, detail="No image in review")

    latest.status = AssetStatus.APPROVED

    if shot.shot_type == ShotType.VEO3_CLIP:
        shot.status = AssetStatus.PENDING  # Ready for video gen
    else:
        shot.status = AssetStatus.APPROVED

    await db.commit()
    return {"ok": True}


@router.post("/shot/{shot_id}/approve-video")
async def approve_shot_video(shot_id: int, db: AsyncSession = Depends(get_db)):
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
    shot = await _get_shot(shot_id, db)
    latest = shot.latest_video_gen
    if latest:
        latest.status = AssetStatus.REJECTED
    shot.status = AssetStatus.PENDING
    shot.video_path = None
    shot.video_url = None
    await db.commit()
    return {"ok": True}


# ── Character Approve / Reject ──────────────────────────────────────

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


# ── Batch Operations ────────────────────────────────────────────────

@router.post("/episode/{episode_id}/generate-all-characters")
async def generate_all_characters(episode_id: int, db: AsyncSession = Depends(get_db)):
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


# ── Import External Task ────────────────────────────────────────────

@router.post("/shot/{shot_id}/import-task")
async def import_task(
    shot_id: int,
    payload: dict = Body(...),
    db: AsyncSession = Depends(get_db),
):
    """
    Import an image generated outside the app by task ID.
    Immediately polls kie.ai for the result and downloads it.
    Body: {"task_id": "...", "gen_type": "image"|"video"}
    """
    task_id = payload.get("task_id", "").strip()
    gen_type_str = payload.get("gen_type", "image")
    if not task_id:
        raise HTTPException(status_code=400, detail="task_id is required")

    result = await db.execute(
        select(Shot).where(Shot.id == shot_id)
        .options(selectinload(Shot.episode), selectinload(Shot.generations))
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404)

    is_video = gen_type_str == "video"

    if is_video:
        gen_type = GenerationType.VIDEO
    elif shot.shot_type == ShotType.VEO3_CLIP:
        gen_type = GenerationType.START_FRAME
    else:
        gen_type = GenerationType.STILL

    # Create generation record with the external task_id
    gen = Generation(
        shot_id=shot.id,
        gen_type=gen_type,
        status=AssetStatus.GENERATING,
        task_id=task_id,
        prompt_used="[imported externally]",
    )
    db.add(gen)
    shot.status = AssetStatus.GENERATING
    await db.commit()
    await db.refresh(gen)

    # Try to poll immediately
    try:
        if is_video:
            task_status = await kie.get_video_status(task_id)
        else:
            task_status = await kie.get_image_status(task_id)

        if task_status.done and task_status.result_urls:
            from pathlib import Path
            ep_slug = shot.episode.slug
            shot_dir = settings.asset_path / ep_slug / "Assets" / f"shot-{shot.number:02d}"
            shot_dir.mkdir(parents=True, exist_ok=True)

            ext = ".mp4" if is_video else ".png"
            filename = f"shot-{shot.number:02d}_{gen_type.value}_{gen.id}{ext}"
            save_path = shot_dir / filename

            dl_ok = await kie.download_file(task_status.result_urls[0], str(save_path))
            if dl_ok:
                gen.result_url = task_status.result_urls[0]
                gen.local_path = str(save_path)
                gen.status = AssetStatus.REVIEW
                gen.completed_at = datetime.now(timezone.utc)

                if is_video:
                    shot.video_path = str(save_path)
                    shot.video_url = task_status.result_urls[0]
                else:
                    shot.image_path = str(save_path)
                    shot.image_url = task_status.result_urls[0]

                shot.status = AssetStatus.REVIEW
                await db.commit()
                return {"ok": True, "status": "downloaded", "path": str(save_path)}

        if task_status.failed:
            gen.status = AssetStatus.FAILED
            gen.error_message = task_status.error or "Task failed"
            shot.status = AssetStatus.FAILED
            await db.commit()
            return {"ok": False, "status": "failed", "error": task_status.error}

    except Exception as e:
        pass  # Poller will pick it up

    await db.commit()
    return {"ok": True, "status": "queued", "detail": "Task queued for polling"}


# ── Preview Payload ─────────────────────────────────────────────────

@router.get("/shot/{shot_id}/preview")
async def preview_payload(
    shot_id: int,
    db: AsyncSession = Depends(get_db),
):
    """
    Preview the exact payload that would be sent to kie.ai for this shot.
    Returns the full request body for both image and video generation.
    """
    result = await db.execute(
        select(Shot)
        .where(Shot.id == shot_id)
        .options(selectinload(Shot.episode).selectinload(Episode.characters))
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404)

    ref_urls = _get_reference_urls(shot)

    preview = {
        "shot_id": shot.id,
        "shot_number": shot.number,
        "shot_name": shot.name,
        "shot_type": shot.shot_type.value,
    }

    # Image payload
    if shot.nano_prompt:
        image_payload = {
            "url": f"{settings.kie_api_base}/api/v1/jobs/createTask",
            "body": {
                "model": settings.get("default_image_model") or settings.default_image_model,
                "prompt": shot.nano_prompt,
                "aspect_ratio": "16:9",
                "resolution": "2K",
                "output_format": "png",
            }
        }
        if ref_urls:
            image_payload["body"]["image_input"] = [{"url": u} for u in ref_urls]
        preview["image_request"] = image_payload

    # Video payload (only for veo3 clips)
    if shot.shot_type == ShotType.VEO3_CLIP and shot.veo3_prompt:
        video_payload = {
            "url": f"{settings.kie_api_base}/api/v1/veo/generate",
            "body": {
                "model": settings.get("default_video_model") or settings.default_video_model,
                "prompt": shot.veo3_prompt,
                "generationType": "FIRST_AND_LAST_FRAMES_2_VIDEO" if shot.image_url else "TEXT_2_VIDEO",
            }
        }
        if shot.image_url:
            video_payload["body"]["imageUrls"] = [shot.image_url]
        preview["video_request"] = video_payload

    # Character references used
    preview["character_references"] = []
    if shot.episode and shot.episode.characters:
        char_map = {c.name.lower(): c for c in shot.episode.characters}
        for ref_name in (shot.character_refs or []):
            char = char_map.get(ref_name.lower())
            if char:
                preview["character_references"].append({
                    "name": char.name,
                    "status": char.status.value,
                    "has_url": bool(char.reference_image_url),
                    "url": char.reference_image_url or None,
                })

    return preview


# ── Helpers ─────────────────────────────────────────────────────────

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
