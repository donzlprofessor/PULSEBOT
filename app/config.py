from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: SecretStr = Field(min_length=1)
    database_url: str = "sqlite+aiosqlite:///./pulsebot.db"
    http_timeout_seconds: float = Field(default=10, gt=0, le=60)
    max_redirects: int = Field(default=3, ge=0, le=10)
    max_concurrent_checks: int = Field(default=10, ge=1, le=100)
    monitor_interval_seconds: float = Field(default=60, ge=1, le=86400)
