from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.filters.command import CommandObject
from aiogram.types import Chat, Message, User

from app.checker import CheckResult
from app.handlers import endpoint_command, router
from app.monitor import MonitoringService


def message(user_id=10, chat_type="private"):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(type=chat_type),
        answer=AsyncMock(),
    )


async def run(repository, action, args=None, user_id=10):
    msg = message(user_id)
    checker = AsyncMock()
    checker.check.return_value = CheckResult(200, 12.5)
    monitor = MonitoringService(repository, checker, None)
    await endpoint_command(msg, CommandObject(command=action, args=args), repository, monitor)
    return msg, checker


@pytest.mark.parametrize("command", ["start", "help"])
async def test_start_and_help_describe_phase2(command, monkeypatch):
    answer = AsyncMock()
    monkeypatch.setattr(Message, "answer", answer)
    msg = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=10, type="private"),
        from_user=User(id=10, is_bot=False, first_name="Test"),
        text=f"/{command}",
    )
    await router.propagate_event(update_type="message", event=msg, bot=AsyncMock())
    answer.assert_awaited_once()
    text = answer.await_args.args[0]
    for name in ("start", "help", "add", "list", "remove", "check", "status", "history"):
        assert any(line.startswith(f"/{name} ") for line in text.splitlines())
    assert "every 60 seconds by default while PULSEBOT is running" in text
    assert "DOWN alerts when an endpoint fails" in text
    assert "UP alerts when it recovers" in text
    assert "first check establishes a baseline" in text
    assert "unchanged states send no alerts" in text
    assert "last 10 checks (UTC)" in text
    assert "private chat" in text
    assert "public HTTPS endpoints on port 443" in text
    assert "private and local addresses are blocked" in text
    assert "Do not put secrets in URL paths or query parameters" in text
    assert "24/7" not in text


async def test_add_list_check_remove(repository):
    msg, _ = await run(repository, "add", "https://example.com")
    assert "Saved endpoint #" in msg.answer.call_args.args[0]
    endpoint = (await repository.list(10))[0]
    msg, _ = await run(repository, "list")
    assert endpoint.url in msg.answer.call_args.args[0]
    msg, checker = await run(repository, "check", str(endpoint.id))
    checker.check.assert_awaited_once_with(endpoint.url)
    assert "HTTP 200" in msg.answer.call_args.args[0]
    msg, _ = await run(repository, "remove", str(endpoint.id))
    assert msg.answer.call_args.args[0] == "Endpoint removed."


async def test_cross_user_check_remove(repository):
    endpoint = await repository.add(10, "https://example.com")
    for action in ("check", "remove"):
        msg, checker = await run(repository, action, str(endpoint.id), user_id=20)
        assert msg.answer.call_args.args[0] == "Endpoint not found."
        checker.check.assert_not_awaited()
    assert await repository.get(10, endpoint.id)


@pytest.mark.parametrize("args", [None, "-1", "0", "abc", "1 2", "9" * 30, "١"])
@pytest.mark.parametrize("action", ["check", "remove"])
async def test_invalid_ids(repository, action, args):
    msg, checker = await run(repository, action, args)
    assert "Usage:" in msg.answer.call_args.args[0]
    checker.check.assert_not_awaited()


async def test_unsafe_add_and_duplicate(repository):
    msg, _ = await run(repository, "add", "https://127.0.0.1")
    assert "public" in msg.answer.call_args.args[0]
    assert await repository.list(10) == []
    await run(repository, "add", "https://example.com")
    msg, _ = await run(repository, "add", "https://example.com")
    assert "already monitor" in msg.answer.call_args.args[0]


async def test_check_error_and_http_failure(repository):
    endpoint = await repository.add(10, "https://example.com")
    for result, expected in [
        (CheckResult(None, 10, "The endpoint timed out."), "timed out"),
        (CheckResult(503, 10), "Unhealthy"),
    ]:
        msg = message()
        checker = AsyncMock()
        checker.check.return_value = result
        await endpoint_command(
            msg,
            CommandObject(command="check", args=str(endpoint.id)),
            repository,
            MonitoringService(repository, checker, None),
        )
        assert expected in msg.answer.call_args.args[0]


async def test_group_rejected(repository):
    msg = message(chat_type="group")
    checker = AsyncMock()
    await endpoint_command(
        msg, CommandObject(command="list"), repository, MonitoringService(repository, checker, None)
    )
    assert "private chat" in msg.answer.call_args.args[0]


async def test_empty_list(repository):
    msg, _ = await run(repository, "list")
    assert "No endpoints" in msg.answer.call_args.args[0]


async def test_status_history_and_ownership(repository):
    endpoint = await repository.add(10, "https://example.com")
    msg, _ = await run(repository, "status")
    assert "UNKNOWN" in msg.answer.call_args.args[0]
    msg, _ = await run(repository, "history", str(endpoint.id))
    assert "No checks" in msg.answer.call_args.args[0]
    await run(repository, "check", str(endpoint.id))
    msg, checker = await run(repository, "status")
    assert "UP" in msg.answer.call_args.args[0]
    checker.check.assert_not_awaited()
    msg, _ = await run(repository, "history", str(endpoint.id))
    assert "HTTP 200" in msg.answer.call_args.args[0]
    msg, _ = await run(repository, "history", str(endpoint.id), user_id=20)
    assert msg.answer.call_args.args[0] == "Endpoint not found."
    msg, _ = await run(repository, "status", user_id=20)
    assert "No endpoints" in msg.answer.call_args.args[0]


async def test_history_limit_and_busy_message(repository):
    endpoint = await repository.add(10, "https://example.com")
    checker = AsyncMock()
    checker.check.return_value = CheckResult(200, 12)
    monitor = MonitoringService(repository, checker, None)
    for _ in range(12):
        await monitor.check(endpoint)
    assert len(await repository.history(10, endpoint.id)) == 10
    monitor.busy.add(endpoint.id)
    msg = message()
    await endpoint_command(
        msg, CommandObject(command="check", args=str(endpoint.id)), repository, monitor
    )
    assert "already running" in msg.answer.call_args.args[0]
