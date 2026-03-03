from pydantic_settings import BaseSettings
from pathlib import Path


class Settings(BaseSettings):
    kie_api_key: str = ""
    kie_api_base: str = "https://api.kie.ai"
    kie_upload_base: str = "https://kieai.redpandaai.co"

    app_host: str = "0.0.0.0"
    app_port: int = 8499
    database_url: str = "sqlite+aiosqlite:////data/studio.db"

    asset_dir: str = "/episodes"
    callback_base_url: str = ""
    poll_interval: int = 15

    default_image_model: str = "nano-banana-pro"
    default_video_model: str = "veo3_fast"

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @property
    def asset_path(self) -> Path:
        p = Path(self.asset_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def callback_url(self) -> str | None:
        if self.callback_base_url:
            return f"{self.callback_base_url.rstrip('/')}/api/callbacks/kie"
        return None


settings = Settings()
