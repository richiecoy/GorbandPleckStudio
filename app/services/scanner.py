"""
Filesystem scanner - discovers episodes from the mounted episodes directory.
Reads episode subdirectories matching ep{NN}-{slug}/ pattern.
"""
import re
import logging
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models import Episode, Shot, Character, ShotType, AssetStatus
from app.services.parser import parse_visual_plan

logger = logging.getLogger(__name__)

EP_PATTERN = re.compile(r'^ep(\d+)-(.+)$', re.IGNORECASE)


async def scan_episodes(db: AsyncSession) -> dict:
    """
    Scan the episodes directory for episode folders.
    Creates new Episode records, updates changed visual plans, auto-parses.
    """
    episodes_dir = settings.asset_path
    summary = {"found": 0, "created": 0, "updated": 0, "parsed": 0, "errors": []}

    if not episodes_dir.is_dir():
        summary["errors"].append(f"Episodes directory not found: {episodes_dir}")
        return summary

    # Get existing episodes keyed by slug
    result = await db.execute(
        select(Episode).options(
            selectinload(Episode.shots),
            selectinload(Episode.characters),
        )
    )
    existing = {ep.slug: ep for ep in result.scalars().all()}

    # Scan filesystem
    for entry in sorted(episodes_dir.iterdir()):
        if not entry.is_dir():
            continue

        match = EP_PATTERN.match(entry.name)
        if not match:
            continue

        summary["found"] += 1
        ep_number = int(match.group(1))
        slug = entry.name

        visual_plan_content = _read_visual_plan(entry)

        if slug in existing:
            episode = existing[slug]
            if visual_plan_content and visual_plan_content != episode.visual_plan_raw:
                episode.visual_plan_raw = visual_plan_content
                episode.parsed_at = None  # Force re-parse
                summary["updated"] += 1
                logger.info(f"Updated visual plan for {slug}")
        else:
            raw_title = match.group(2).replace("-", " ").title()
            episode = Episode(
                number=ep_number,
                slug=slug,
                title=raw_title,
                visual_plan_raw=visual_plan_content or "",
            )
            db.add(episode)
            summary["created"] += 1
            logger.info(f"Discovered new episode: {slug}")

    await db.commit()

    # Auto-parse any episodes with visual plans that haven't been parsed
    result = await db.execute(
        select(Episode)
        .where(Episode.visual_plan_raw != "")
        .where(Episode.parsed_at.is_(None))
        .options(
            selectinload(Episode.shots),
            selectinload(Episode.characters),
        )
    )
    unparsed = result.scalars().all()

    for episode in unparsed:
        try:
            await _auto_parse(episode, db)
            summary["parsed"] += 1
            logger.info(f"Auto-parsed {episode.slug}")
        except Exception as e:
            summary["errors"].append(f"Parse error for {episode.slug}: {e}")
            logger.error(f"Failed to parse {episode.slug}: {e}")

    await db.commit()

    # Link existing assets for all episodes
    result = await db.execute(
        select(Episode).options(
            selectinload(Episode.shots),
            selectinload(Episode.characters),
        )
    )
    all_episodes = result.scalars().all()
    for episode in all_episodes:
        try:
            await _link_existing_assets(episode, db)
        except Exception as e:
            logger.error(f"Asset linking error for {episode.slug}: {e}")

    await db.commit()
    return summary


def _read_visual_plan(episode_dir: Path) -> str | None:
    """Find and read visual-plan.md from an episode directory (case-insensitive)."""
    for candidate in episode_dir.iterdir():
        if candidate.is_file() and candidate.name.lower() == "visual-plan.md":
            try:
                return candidate.read_text(encoding="utf-8")
            except Exception as e:
                logger.error(f"Failed to read {candidate}: {e}")
                return None
    return None


async def _auto_parse(episode: Episode, db: AsyncSession):
    """Parse visual plan and create shots + characters for an episode."""
    parsed = parse_visual_plan(episode.visual_plan_raw)

    if parsed.title:
        episode.title = parsed.title
    if parsed.location:
        episode.location = parsed.location

    # Clear existing shots/characters if re-parsing
    for shot in list(episode.shots):
        await db.delete(shot)
    for char in list(episode.characters):
        if not char.is_main:
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


async def _link_existing_assets(episode: Episode, db: AsyncSession):
    """
    Scan the episode's Assets/ folder for existing image/video files
    and link them to the matching shots and characters.
    """
    import re
    ep_dir = settings.asset_path / episode.slug

    # Find assets dir (case-insensitive)
    assets_dir = None
    for entry in ep_dir.iterdir():
        if entry.is_dir() and entry.name.lower() == "assets":
            assets_dir = entry
            break
    if not assets_dir:
        return

    # ── Link character reference images ──
    chars_dir = None
    for entry in assets_dir.iterdir():
        if entry.is_dir() and entry.name.lower() == "characters":
            chars_dir = entry
            break

    if chars_dir:
        char_map = {c.name.lower(): c for c in episode.characters}
        for f in chars_dir.iterdir():
            if not f.is_file() or f.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp"):
                continue
            # Skip cropped versions
            if "crop" in f.stem.lower():
                continue
            # Match by character name in filename
            fname_lower = f.stem.lower()
            for name, char in char_map.items():
                if name in fname_lower and not char.reference_image_path:
                    # Store path relative to the asset_dir mount
                    rel_path = str(f.relative_to(settings.asset_path))
                    char.reference_image_path = rel_path
                    char.status = AssetStatus.APPROVED
                    logger.info(f"Linked existing character ref: {char.name} -> {rel_path}")
                    break

    # ── Link shot images and videos ──
    shot_pattern = re.compile(r'(?:Shot|Still)\s*(\d+)', re.IGNORECASE)
    shot_map = {s.number: s for s in episode.shots}

    for f in _walk_media_files(assets_dir):
        match = shot_pattern.search(f.name)
        if not match:
            continue
        shot_num = int(match.group(1))
        shot = shot_map.get(shot_num)
        if not shot:
            continue

        rel_path = str(f.relative_to(settings.asset_path))
        ext = f.suffix.lower()

        if ext in (".png", ".jpg", ".jpeg", ".webp") and not shot.image_path:
            shot.image_path = rel_path
            if shot.status == AssetStatus.PENDING:
                shot.status = AssetStatus.APPROVED
            logger.info(f"Linked existing image: shot {shot_num} -> {rel_path}")
        elif ext == ".mp4" and not shot.video_path:
            shot.video_path = rel_path
            if shot.needs_video and shot.status == AssetStatus.PENDING:
                shot.status = AssetStatus.APPROVED
            logger.info(f"Linked existing video: shot {shot_num} -> {rel_path}")


def _walk_media_files(directory: Path):
    """Recursively yield image and video files from a directory."""
    MEDIA_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".mp4"}
    for entry in sorted(directory.iterdir()):
        if entry.is_dir() and entry.name.lower() != "characters":
            yield from _walk_media_files(entry)
        elif entry.is_file() and entry.suffix.lower() in MEDIA_EXTS:
            yield entry
