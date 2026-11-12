import pytest
from pydantic import ValidationError

from app.config import Settings


def test_environment_and_secret_redaction(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "test-placeholder-not-a-real-token")
    monkeypatch.setenv("HTTP_TIMEOUT_SECONDS", "4")
    settings = Settings(_env_file=None)
    assert settings.http_timeout_seconds == 4
    assert "test-placeholder" not in repr(settings)


def test_missing_token(monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_monitor_interval_default_and_validation(monkeypatch):
    monkeypatch.setenv("BOT_TOKEN", "test-placeholder-not-a-real-token")
    monkeypatch.delenv("MONITOR_INTERVAL_SECONDS", raising=False)
    assert Settings(_env_file=None).monitor_interval_seconds == 60
    monkeypatch.setenv("MONITOR_INTERVAL_SECONDS", "5")
    assert Settings(_env_file=None).monitor_interval_seconds == 5
    monkeypatch.setenv("MONITOR_INTERVAL_SECONDS", "0")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
