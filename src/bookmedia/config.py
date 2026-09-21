"""Environment-only configuration.

Secrets and paths come from environment variables (see .env.example).
Never log values loaded here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    bot_token: str
    allowed_user_ids: tuple[str, ...] = ()
    data_dir: str = "/app/data"
    temp_dir: str = "/app/tmp"
    cookies_path: str = "/app/cookies/cookies.txt"
    log_level: str = "INFO"
    local_bot_api_host: str = ""
    local_bot_api_port: int = 8081
    dashboard_port: int = 0
    max_media_bytes: int = 2000 * 1024 * 1024
    db_path: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "db_path", os.path.join(self.data_dir, "archive.db")
        )

    @property
    def local_bot_api_url(self) -> str:
        """Base URL for the local Bot API server, or empty string for cloud."""
        if not self.local_bot_api_host:
            return ""
        return f"http://{self.local_bot_api_host}:{self.local_bot_api_port}"


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """Load settings. ``env`` overrides os.environ (used by tests)."""
    source = dict(os.environ) if env is None else env

    def get(name: str, default: str = "") -> str:
        return source.get(name, default).strip()

    bot_token = get("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        raise ValueError("missing required environment variable: TELEGRAM_BOT_TOKEN")

    raw_ids = get("TELEGRAM_ALLOWED_USER_IDS")
    allowed = tuple(p for p in (x.strip() for x in raw_ids.split(",")) if p) if raw_ids else ()

    local_host = get("LOCAL_BOT_API_HOST")
    local_port_str = get("LOCAL_BOT_API_PORT", "8081")
    try:
        local_port = int(local_port_str)
    except ValueError:
        local_port = 8081

    dashboard_port_str = get("DASHBOARD_PORT", "0")
    try:
        dashboard_port = int(dashboard_port_str)
    except ValueError:
        dashboard_port = 0

    max_media_str = get("MAX_MEDIA_BYTES", "2097152000")
    try:
        max_media_bytes = int(max_media_str)
    except ValueError:
        max_media_bytes = 2000 * 1024 * 1024
    if max_media_bytes < 0:
        max_media_bytes = 0

    return Settings(
        bot_token=bot_token,
        allowed_user_ids=allowed,
        data_dir=get("DATA_DIR", "/app/data"),
        temp_dir=get("TEMP_DIR", "/app/tmp"),
        cookies_path=get("COOKIES_PATH", "/app/cookies/cookies.txt"),
        log_level=get("LOG_LEVEL", "INFO").upper() or "INFO",
        local_bot_api_host=local_host,
        local_bot_api_port=local_port,
        dashboard_port=dashboard_port,
        max_media_bytes=max_media_bytes,
    )
