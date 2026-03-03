"""Popup window routes for Import and Preview."""
import json

from fastapi import APIRouter, Depends, Request, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models import Shot, Character, Episode, AssetStatus, ShotType
from app.config import settings

router = APIRouter(prefix="/popup")


@router.get("/preview/shot/{shot_id}", response_class=HTMLResponse)
async def preview_shot_popup(
    request: Request,
    shot_id: int,
    mode: str = Query("image", regex="^(image|video)$"),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Shot).where(Shot.id == shot_id)
        .options(selectinload(Shot.episode).selectinload(Episode.characters))
    )
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404)

    if mode == "video":
        preview = _build_video_preview(shot)
        title = f"Shot #{shot.number} — Video Prompt"
    else:
        ref_urls = _get_reference_urls(shot)
        preview = _build_image_preview(shot, ref_urls)
        title = f"Shot #{shot.number} — Image Payload"

    return request.app.state.templates.TemplateResponse("popup_preview.html", {
        "request": request,
        "title": title,
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
async def import_shot_popup(
    request: Request,
    shot_id: int,
    mode: str = Query("image", regex="^(image|video)$"),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Shot).where(Shot.id == shot_id))
    shot = result.scalar_one_or_none()
    if not shot:
        raise HTTPException(status_code=404)

    return request.app.state.templates.TemplateResponse("popup_import.html", {
        "request": request,
        "title": f"Shot #{shot.number} {shot.name}",
        "target_type": "shot",
        "target_id": shot.id,
        "import_mode": mode,
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
        "import_mode": "image",
    })


# ── Helpers ──────────────────────────────────────────────────────────

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


def _build_image_preview(shot: Shot, ref_urls: list[str]) -> dict:
    """Build the exact kie.ai createTask payload that would be sent."""
    if not shot.nano_prompt:
        return {"note": "No Nano Banana prompt set for this shot."}

    payload = {
        "model": settings.get("default_image_model") or settings.default_image_model,
        "input": {
            "prompt": shot.nano_prompt,
            "image_input": ref_urls if ref_urls else [],
            "aspect_ratio": "16:9",
            "resolution": "2K",
            "output_format": "png",
        }
    }

    cb = settings.callback_url
    if cb:
        payload["callBackUrl"] = cb

    return payload


def _build_video_preview(shot: Shot) -> dict:
    """Build video preview — just the prompt."""
    return {
        "shot": f"#{shot.number} — {shot.name}",
        "veo3_prompt": shot.veo3_prompt or "(no Veo3 prompt set)",
        "start_frame": "approved" if shot.image_url else "not yet approved",
    }
