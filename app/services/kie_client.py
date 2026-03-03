"""
Kie.ai API client for Nano Banana image generation and Veo 3 video generation.

All operations are async. Generation tasks return a task_id that must be
polled for results (or use callbacks).
"""
import httpx
import logging
from pathlib import Path
from dataclasses import dataclass

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class TaskResult:
    task_id: str
    success: bool
    error: str | None = None


@dataclass
class TaskStatus:
    task_id: str
    status: str          # "pending", "processing", "success", "failed"
    result_urls: list[str] | None = None
    error: str | None = None


class KieClient:
    """Client for kie.ai image and video generation APIs."""

    def __init__(self):
        self.base = settings.kie_api_base
        self.upload_base = settings.kie_upload_base
        self.headers = {
            "Authorization": f"Bearer {settings.kie_api_key}",
            "Content-Type": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=60.0, headers=self.headers)

    # ── File Upload ──────────────────────────────────────────────────

    async def upload_file(self, file_path: str, upload_path: str = "gorb-pleck") -> str | None:
        """Upload a local file to kie.ai temp storage. Returns the download URL."""
        path = Path(file_path)
        if not path.exists():
            logger.error(f"File not found: {file_path}")
            return None

        async with httpx.AsyncClient(timeout=120.0) as client:
            with open(path, "rb") as f:
                resp = await client.post(
                    f"{self.upload_base}/api/file-stream-upload",
                    headers={"Authorization": f"Bearer {settings.kie_api_key}"},
                    files={"file": (path.name, f, _mime_type(path))},
                    data={
                        "uploadPath": upload_path,
                        "fileName": path.name,
                    },
                )

            if resp.status_code == 200:
                data = resp.json()
                if data.get("success") or data.get("code") == 200:
                    url = data.get("data", {}).get("downloadUrl")
                    logger.info(f"Uploaded {path.name} → {url}")
                    return url

            logger.error(f"Upload failed ({resp.status_code}): {resp.text}")
            return None

    # ── Image Generation (Nano Banana) ───────────────────────────────

    async def generate_image(
        self,
        prompt: str,
        reference_urls: list[str] | None = None,
        model: str | None = None,
        aspect_ratio: str = "16:9",
        resolution: str = "1K",
    ) -> TaskResult:
        """Submit an image generation task. Returns task_id."""
        model = model or settings.default_image_model

        payload = {
            "model": model,
            "input": {
                "prompt": prompt,
                "image_input": reference_urls or [],
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
                "output_format": "png",
            },
        }

        if settings.callback_url:
            payload["callBackUrl"] = settings.callback_url

        async with self._client() as client:
            try:
                resp = await client.post(
                    f"{self.base}/api/v1/jobs/createTask",
                    json=payload,
                )
                data = resp.json()

                if resp.status_code == 200 and data.get("code") == 200:
                    task_id = data["data"]["taskId"]
                    logger.info(f"Image task created: {task_id} (model={model})")
                    return TaskResult(task_id=task_id, success=True)
                else:
                    err = data.get("msg", f"HTTP {resp.status_code}")
                    logger.error(f"Image generation failed: {err}")
                    return TaskResult(task_id="", success=False, error=err)
            except Exception as e:
                logger.error(f"Image generation error: {e}")
                return TaskResult(task_id="", success=False, error=str(e))

    async def get_image_status(self, task_id: str) -> TaskStatus:
        """Poll image generation task status."""
        async with self._client() as client:
            try:
                resp = await client.get(
                    f"{self.base}/api/v1/jobs/record-info",
                    params={"taskId": task_id},
                )
                data = resp.json()

                if data.get("code") == 200:
                    task_data = data.get("data", {})
                    info = task_data.get("info", {})
                    status_val = task_data.get("status", "")

                    # Map kie.ai statuses to our simplified model
                    if status_val in ("success", "completed"):
                        urls = info.get("images") or info.get("resultUrls") or []
                        if isinstance(urls, str):
                            urls = [urls]
                        return TaskStatus(
                            task_id=task_id, status="success", result_urls=urls
                        )
                    elif status_val in ("failed", "error"):
                        return TaskStatus(
                            task_id=task_id, status="failed",
                            error=info.get("errorMessage", "Generation failed")
                        )
                    else:
                        return TaskStatus(task_id=task_id, status="processing")

                return TaskStatus(
                    task_id=task_id, status="failed",
                    error=data.get("msg", "Unknown error")
                )
            except Exception as e:
                logger.error(f"Image status poll error: {e}")
                return TaskStatus(task_id=task_id, status="failed", error=str(e))

    # ── Video Generation (Veo 3) ─────────────────────────────────────

    async def generate_video(
        self,
        prompt: str,
        image_urls: list[str] | None = None,
        model: str | None = None,
        aspect_ratio: str = "16:9",
        generation_type: str | None = None,
    ) -> TaskResult:
        """Submit a video generation task. Returns task_id."""
        model = model or settings.default_video_model

        payload = {
            "prompt": prompt,
            "model": model,
            "aspect_ratio": aspect_ratio,
            "enableTranslation": False,  # Prompts are already English
        }

        if image_urls:
            payload["imageUrls"] = image_urls
            # Auto-detect generation type if not specified
            if not generation_type:
                generation_type = "FIRST_AND_LAST_FRAMES_2_VIDEO"
            payload["generationType"] = generation_type

        if settings.callback_url:
            payload["callBackUrl"] = settings.callback_url

        async with self._client() as client:
            try:
                resp = await client.post(
                    f"{self.base}/api/v1/veo/generate",
                    json=payload,
                )
                data = resp.json()

                if resp.status_code == 200 and data.get("code") == 200:
                    task_id = data["data"]["taskId"]
                    logger.info(f"Video task created: {task_id} (model={model})")
                    return TaskResult(task_id=task_id, success=True)
                else:
                    err = data.get("msg", f"HTTP {resp.status_code}")
                    logger.error(f"Video generation failed: {err}")
                    return TaskResult(task_id="", success=False, error=err)
            except Exception as e:
                logger.error(f"Video generation error: {e}")
                return TaskResult(task_id="", success=False, error=str(e))

    async def get_video_status(self, task_id: str) -> TaskStatus:
        """Poll video generation task status."""
        async with self._client() as client:
            try:
                resp = await client.get(
                    f"{self.base}/api/v1/veo/record-info",
                    params={"taskId": task_id},
                )
                data = resp.json()

                if data.get("code") == 200:
                    task_data = data.get("data", {})
                    info = task_data.get("info", {})
                    status_val = task_data.get("status", "")

                    if status_val in ("success", "completed"):
                        urls = info.get("resultUrls") or []
                        if isinstance(urls, str):
                            import json as _json
                            try:
                                urls = _json.loads(urls)
                            except Exception:
                                urls = [urls]
                        return TaskStatus(
                            task_id=task_id, status="success", result_urls=urls
                        )
                    elif status_val in ("failed", "error"):
                        return TaskStatus(
                            task_id=task_id, status="failed",
                            error=info.get("errorMessage", "Generation failed")
                        )
                    else:
                        return TaskStatus(task_id=task_id, status="processing")

                return TaskStatus(
                    task_id=task_id, status="failed",
                    error=data.get("msg", "Unknown error")
                )
            except Exception as e:
                logger.error(f"Video status poll error: {e}")
                return TaskStatus(task_id=task_id, status="failed", error=str(e))

    # ── Download Generated Assets ────────────────────────────────────

    async def download_file(self, url: str, save_path: str) -> bool:
        """Download a generated asset from kie.ai temp URL to local storage."""
        path = Path(save_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(timeout=120.0) as client:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    path.write_bytes(resp.content)
                    logger.info(f"Downloaded {url} → {save_path}")
                    return True
                else:
                    logger.error(f"Download failed ({resp.status_code}): {url}")
                    return False
            except Exception as e:
                logger.error(f"Download error: {e}")
                return False


def _mime_type(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
    }.get(suffix, "application/octet-stream")


# Module-level singleton
kie = KieClient()
