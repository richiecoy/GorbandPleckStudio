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
            .options(selectinload(Generation.shot), selectinload(Generation.character))
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

                if status.status == "success" and status.result_urls:
                    # Download the first result
                    url = status.result_urls[0]
                    gen.result_url = url

                    # Determine save path
                    save_path = _asset_path(gen)
                    downloaded = await kie.download_file(url, save_path)

                    if downloaded:
                        gen.local_path = save_path
                        gen.status = AssetStatus.REVIEW
                        gen.completed_at = datetime.now(timezone.utc)

                        # Update parent shot or character
                        if gen.shot:
                            if gen.gen_type == GenerationType.VIDEO:
                                gen.shot.video_path = save_path
                                gen.shot.video_url = url
                            else:
                                gen.shot.image_path = save_path
                                gen.shot.image_url = url
                            gen.shot.status = AssetStatus.REVIEW
                        elif gen.character:
                            gen.character.reference_image_path = save_path
                            gen.character.reference_image_url = url
                            gen.character.status = AssetStatus.REVIEW
                    else:
                        gen.status = AssetStatus.FAILED
                        gen.error_message = "Download failed"

                elif status.status == "failed":
                    gen.status = AssetStatus.FAILED
                    gen.error_message = status.error or "Generation failed"
                    gen.completed_at = datetime.now(timezone.utc)

                    if gen.shot:
                        gen.shot.status = AssetStatus.FAILED
                    elif gen.character:
                        gen.character.status = AssetStatus.FAILED

                # "processing" = still running, no change needed

            except Exception as e:
                logger.error(f"Error polling generation {gen.id}: {e}")

        await db.commit()


def _asset_path(gen: Generation) -> str:
    """Determine local save path for a generated asset."""
    base = settings.asset_path

    if gen.character:
        folder = base / "characters"
        ext = ".png"
        name = gen.character.name.lower().replace(" ", "-")
        return str(folder / f"{name}_{gen.id}{ext}")

    if gen.shot:
        ep = gen.shot.episode
        folder = base / "episodes" / f"ep{ep.number:02d}" / f"shot-{gen.shot.number:02d}"
        folder.mkdir(parents=True, exist_ok=True)

        if gen.gen_type == GenerationType.VIDEO:
            ext = ".mp4"
            return str(folder / f"shot-{gen.shot.number:02d}_video_{gen.id}{ext}")
        else:
            ext = ".png"
            return str(folder / f"shot-{gen.shot.number:02d}_image_{gen.id}{ext}")

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
