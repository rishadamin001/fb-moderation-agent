"""Environment configuration shared by the server and API clients."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent


def _boolean(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True)
class Settings:
    openai_api_key: str = field(default="", repr=False)
    fb_page_access_token: str = field(default="", repr=False)
    fb_verify_token: str = field(default="", repr=False)
    fb_app_secret: str = field(default="", repr=False)
    fb_page_id: str | None = None
    openai_model: str = "gpt-4o-mini"
    fb_graph_version: str = "v24.0"
    dry_run: bool = True
    api_timeout_seconds: float = 20.0
    worker_count: int = 2
    queue_capacity: int = 10000
    inbox_path: Path = BASE_DIR / ".local" / "moderation.sqlite3"
    log_path: Path = BASE_DIR / "webhook_moderation.log"
    max_body_bytes: int = 1048576

    def __post_init__(self) -> None:
        version = re.fullmatch(r"v(\d+)\.(\d+)", self.fb_graph_version)
        if version is None or int(version.group(1)) < 20:
            raise ValueError("FB_GRAPH_VERSION must be v20.0 or newer")
        if self.fb_page_id is not None and not re.fullmatch(r"[0-9]+", self.fb_page_id):
            raise ValueError("FB_PAGE_ID must be a numeric Page identifier")
        if not self.openai_model.strip():
            raise ValueError("OPENAI_MODEL must not be empty")
        if not 1 <= self.worker_count <= 16:
            raise ValueError("WORKER_COUNT must be between 1 and 16")
        if not 1 <= self.queue_capacity <= 1000000:
            raise ValueError("QUEUE_CAPACITY must be between 1 and 1000000")
        if not 0 < self.api_timeout_seconds <= 120:
            raise ValueError("API_TIMEOUT_SECONDS must be between 0 and 120")
        if not 1024 <= self.max_body_bytes <= 10485760:
            raise ValueError("MAX_BODY_BYTES must be between 1024 and 10485760")

    @property
    def moderation_ready(self) -> bool:
        return bool(self.openai_api_key and self.fb_app_secret and (self.dry_run or self.fb_page_access_token))

    @classmethod
    def from_env(cls) -> Settings:
        # Process variables take precedence over the local .env file.
        load_dotenv(BASE_DIR / ".env", override=False)
        return cls(
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),
            fb_page_access_token=os.getenv("FB_PAGE_ACCESS_TOKEN", ""),
            fb_verify_token=os.getenv("FB_VERIFY_TOKEN", ""),
            fb_app_secret=os.getenv("FB_APP_SECRET", ""),
            fb_page_id=os.getenv("FB_PAGE_ID", "").strip() or None,
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            fb_graph_version=os.getenv("FB_GRAPH_VERSION", "v24.0"),
            dry_run=_boolean("DRY_RUN", True),
            api_timeout_seconds=float(os.getenv("API_TIMEOUT_SECONDS", "20")),
            worker_count=int(os.getenv("WORKER_COUNT", "2")),
            queue_capacity=int(os.getenv("QUEUE_CAPACITY", "10000")),
            inbox_path=Path(os.getenv("INBOX_PATH", str(BASE_DIR / ".local" / "moderation.sqlite3"))),
            log_path=Path(os.getenv("MODERATION_LOG_PATH", str(BASE_DIR / "webhook_moderation.log"))),
            max_body_bytes=int(os.getenv("MAX_BODY_BYTES", "1048576")),
        )
