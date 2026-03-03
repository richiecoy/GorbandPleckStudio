"""Popup window routes for Import and Preview."""
import json

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Shot, Character, Episode, AssetStatus
from app.config import settings

router = APIRouter(prefix="/popup")


@router.get("/preview/shot/{shot_id}", response_class=HTMLResponse)
async def preview_shot_popup(request: Request, shot_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Shot).where(Shot.id == shot_id)
        .options(selectinload(Shot.episode).selectinload(Episode.characters))
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404)

    ref_urls = _get_reference_urls(shot)
    preview = _build_shot_preview(shot, ref_urls)

    return request.app.state.templates.TemplateResponse("popup_preview.html", {
        "request": request,
        "title": f"Shot #{shot.number} {shot.name}",
        "payload_json": json.dumps(preview, indent=2),
    })


@router.get("/preview/character/{character_id}", response_class=HTMLResponse)
async def preview_character_popup(request: Request, character_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Character).where(Character.id == character_id))
    char = result.scalar_one_or_none()
    if not char:
        raise HTTPException(status_code=404)

    prompt = char.prompt or char.description or ""
    full_prompt = (
        f"Photorealistic 3D CGI render, Pixar-quality. "
        f"Character reference portrait, head and upper body, neutral background. "
        f"{prompt}"
    )

    preview = {
        "character_id": char.id,
        "character_name": char.name,
        "description": char.description,
        "image_request": {
            "url": f"{settings.kie_api_base}/api/v1/jobs/createTask",
            "body": {
                "model": settings.get("default_image_model") or settings.default_image_model,
                "prompt": full_prompt,
                "aspect_ratio": "1:1",
                "resolution": "1K",
                "output_format": "png",
            }
        }
    }

    return request.app.state.templates.TemplateResponse("popup_preview.html", {
        "request": request,
        "title": f"Character: {char.name}",
        "payload_json": json.dumps(preview, indent=2),
    })


@router.get("/import/shot/{shot_id}", response_class=HTMLResponse)
async def import_shot_popup(request: Request, shot_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Shot).where(Shot.id == shot_id))
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404)

    return request.app.state.templates.TemplateResponse("popup_import.html", {
        "request": request,
        "title": f"Shot #{shot.number} {shot.name}",
        "target_type": "shot",
        "target_id": shot.id,
    })


@router.get("/import/character/{character_id}", response_class=HTMLResponse)
async def import_character_popup(request: Request, character_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Character).where(Character.id == character_id))
    char = result.scalar_one_or_none()
    if not char:
        raise HTTPException(status_code=404)

    return request.app.state.templates.TemplateResponse("popup_import.html", {
        "request": request,
        "title": char.name,
        "target_type": "character",
        "target_id": char.id,
    })


# ── Helpers (duplicated from generation.py to avoid circular imports) ──

def _get_reference_urls(shot: Shot) -> list[str]:
    urls = []
    if not shot.episode or not shot.episode.characters:
        return urls
    char_map = {c.name.lower(): c for c in shot.episode.characters}
    for ref_name in (shot.character_refs or []):
        char = char_map.get(ref_name.lower())
        if char and char.reference_image_url and char.status == AssetStatus.APPROVED:
            urls.append(char.reference_image_url)
    return urls


def _build_shot_preview(shot: Shot, ref_urls: list[str]) -> dict:
    from app.models import ShotType
    preview = {
        "shot_id": shot.id,
        "shot_number": shot.number,
        "shot_name": shot.name,
        "shot_type": shot.shot_type.value,
    }

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
                })

    return preview
