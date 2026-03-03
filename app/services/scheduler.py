"""
Background task poller using APScheduler.

Checks pending generation tasks and downloads completed assets.
"""
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.database import async_session
from app.models import Generation, Shot, Character, AssetStatus, GenerationType
from app.services.kie_client import kie
from app.config import settings

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def poll_pending_generations():
    """Check all generating tasks and update status."""
    async with async_session() as db:
        result = await db.execute(
            select(Generation)
            .where(Generation.status == AssetStatus.GENERATING)
            .options(
                selectinload(Generation.shot).selectinload(Shot.episode),
                selectinload(Generation.character).selectinload(Character.episode),
            )
        )
        pending = result.scalars().all()

        if not pending:
            return

        logger.info(f"Polling {len(pending)} pending generations...")

        for gen in pending:
            try:
                if gen.gen_type == GenerationType.VIDEO:
                    status = await kie.get_video_status(gen.task_id)
                else:
                    status = await kie.get_image_status(gen.task_id)

                # Transient poll error — skip this cycle, try again later
                if status.poll_error:
                    logger.warning(
                        f"Poll error for gen {gen.id} (task {gen.task_id}): "
                        f"{status.error} — will retry next cycle"
                    )
                    continue

                if status.done and status.result_urls:
                    # Download the first result
                    url = status.result_urls[0]
                    gen.result_url = url

                    # Determine save path
                    save_path = _asset_path(gen)
                    downloaded = await kie.download_file(url, save_path)

                    if downloaded:
                        from pathlib import Path
                        rel_path = str(Path(save_path).relative_to(settings.asset_path)).replace('\\', '/')
                        gen.local_path = rel_path
                        gen.status = AssetStatus.REVIEW
                        gen.completed_at = datetime.now(timezone.utc)

                        # Update parent shot or character
                        if gen.shot:
                            if gen.gen_type == GenerationType.VIDEO:
                                gen.shot.video_path = rel_path
                                gen.shot.video_url = url
                            else:
                                gen.shot.image_path = rel_path
                                gen.shot.image_url = url
                            gen.shot.status = AssetStatus.REVIEW
                        elif gen.character:
                            gen.character.reference_image_path = rel_path
                            gen.character.reference_image_url = url
                            gen.character.status = AssetStatus.REVIEW

                        logger.info(f"Gen {gen.id} completed → {rel_path}")
                    else:
                        gen.status = AssetStatus.FAILED
                        gen.error_message = "Download failed"
                        logger.error(f"Gen {gen.id}: download failed for {url}")

                elif status.failed:
                    gen.status = AssetStatus.FAILED
                    gen.error_message = status.error or "Generation failed"
                    gen.completed_at = datetime.now(timezone.utc)

                    if gen.shot:
                        gen.shot.status = AssetStatus.FAILED
                    elif gen.character:
                        gen.character.status = AssetStatus.FAILED

                    logger.info(f"Gen {gen.id} failed: {status.error}")

                # "processing" = still running, no change needed

            except Exception as e:
                logger.error(f"Error polling generation {gen.id}: {e}")

        await db.commit()


def _segment_folder_name(segment: str) -> str:
    """Map parser segment names to on-disk folder names."""
    seg = segment.strip().upper()
    if seg == "INTRO":
        return "Intro"
    if seg == "REVIEW":
        return "Review"
    if seg == "POST-CREDITS":
        return "Post-Credits"
    # "STORY SEGMENT 1" -> "Segment #1"
    import re
    m = re.match(r'STORY SEGMENT\s*(\d+)', seg)
    if m:
        return f"Segment #{m.group(1)}"
    return segment.title()


def _asset_path(gen: Generation) -> str:
    """Determine local save path for a generated asset."""
    base = settings.asset_path

    if gen.character:
        # Find the episode this character belongs to
        ep = gen.character.episode
        if ep:
            folder = base / ep.slug / "Assets" / "characters"
        else:
            folder = base / "characters"
        folder.mkdir(parents=True, exist_ok=True)
        ext = ".png"
        name = gen.character.name
        return str(folder / f"{name}{ext}")

    if gen.shot:
        ep = gen.shot.episode
        seg_folder = _segment_folder_name(gen.shot.segment) if gen.shot.segment else "Other"
        folder = base / ep.slug / "Assets" / seg_folder
        folder.mkdir(parents=True, exist_ok=True)

        shot_name = gen.shot.name or f"Shot {gen.shot.number}"
        if gen.gen_type == GenerationType.VIDEO:
            return str(folder / f"Shot {gen.shot.number} - {shot_name}.mp4")
        else:
            return str(folder / f"Shot {gen.shot.number} - {shot_name}.png")

    return str(base / f"gen_{gen.id}.png")


def start_scheduler():
    """Start the background polling scheduler."""
    scheduler.add_job(
        poll_pending_generations,
        "interval",
        seconds=settings.poll_interval,
        id="poll_generations",
        replace_existing=True,
    )
    scheduler.start()
    logger.info(f"Scheduler started (poll interval: {settings.poll_interval}s)")


def stop_scheduler():
    scheduler.shutdown(wait=False)
