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

    # Runtime overrides from DB — populated at startup and on settings save
    _runtime_overrides: dict = {}

    def get(self, key: str) -> str:
        """Get a setting value, checking runtime overrides first."""
        if key in self._runtime_overrides:
            return self._runtime_overrides[key]
        return getattr(self, key, "")

    def set_override(self, key: str, value: str):
        """Set a runtime override (also call save_to_db to persist)."""
        self._runtime_overrides[key] = value

    @property
    def effective_kie_api_key(self) -> str:
        return self._runtime_overrides.get("kie_api_key", self.kie_api_key)

    @property
    def effective_asset_dir(self) -> str:
        return self._runtime_overrides.get("asset_dir", self.asset_dir)

    @property
    def asset_path(self) -> Path:
        p = Path(self.effective_asset_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def callback_url(self) -> str | None:
        if self.callback_base_url:
            return f"{self.callback_base_url.rstrip('/')}/api/callbacks/kie"
        return None


settings = Settings()
