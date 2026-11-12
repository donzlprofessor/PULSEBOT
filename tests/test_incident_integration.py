"""Offline integration of scheduler cycles, SQLite, alert delivery and ownership."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, call

from aiogram import Bot
from aiogram.filters.command import CommandObject
from sqlalchemy import select

from app.checker import CheckResult, HealthChecker
from app.db import Incident, Repository
from app.handlers import endpoint_command
from app.monitor import MonitoringService


async def test_incident_lifecycle_with_restarts_and_owner_isolation(tmp_path):
    database = f"sqlite+aiosqlite:///{tmp_path / 'incident-integration.db'}"
    repository = Repository(database)
    checker = AsyncMock(spec=HealthChecker)
    telegram = AsyncMock(spec=Bot)
    monitor = MonitoringService(repository, checker, telegram)
    observations = [
        CheckResult(200, 12),
        CheckResult(503, 50),
        CheckResult(503, 55),
        CheckResult(200, 14),
    ]
    sequence = iter(observations)
    target_url = "https://example.com/monitored"
    other_url = "https://example.com/other-owner"

    async def mock_http(url):
        if url == target_url:
            return next(sequence)
        assert url == other_url
        return CheckResult(200, 8)

    checker.check.side_effect = mock_http

    async def incident_rows():
        async with repository.sessions() as session:
            return list(await session.scalars(select(Incident).order_by(Incident.id)))

    async def restart():
        nonlocal repository, monitor
        await monitor.stop()
        await repository.close()
        # Reopen the SQLite file and recreate the service, not just its in-memory state.
        repository = Repository(database)
        await repository.initialize()
        monitor = MonitoringService(repository, checker, telegram)

    try:
        await repository.initialize()
        endpoint = await repository.add(101, target_url)
        other = await repository.add(202, other_url)

        # Drive production scheduler cycles directly, without wall-clock sleeps or background races.
        await monitor.run_cycle()  # UP: baseline, no notification.
        assert not await monitor.deliver_one()
        assert await incident_rows() == []
        telegram.send_message.assert_not_awaited()

        await monitor.run_cycle()  # UP -> DOWN: durable pending incident.
        rows = await incident_rows()
        assert [(row.healthy, row.state) for row in rows] == [(False, "pending")]
        await restart()
        assert await monitor.deliver_one()
        assert not await monitor.deliver_one()
        assert telegram.send_message.await_count == 1

        # Restart after delivery: the persisted DOWN baseline must suppress another alert.
        await restart()
        await monitor.run_cycle()  # DOWN -> DOWN: no new incident or notification.
        assert not await monitor.deliver_one()
        assert [(row.healthy, row.state) for row in await incident_rows()] == [(False, "sent")]
        assert telegram.send_message.await_count == 1

        await monitor.run_cycle()  # DOWN -> UP: one recovery event.
        assert [(row.healthy, row.state) for row in await incident_rows()] == [
            (False, "sent"),
            (True, "pending"),
        ]
        await restart()
        assert await monitor.deliver_one()
        assert not await monitor.deliver_one()
        await restart()
        assert not await monitor.deliver_one()  # No replay of either sent event.

        history = list(reversed(await repository.history(101, endpoint.id)))
        assert len(history) == 4
        assert [row.status for row in history] == [200, 503, 503, 200]
        assert [row.healthy for row in history] == [True, False, False, True]
        assert [row.latency_ms for row in history] == [12, 50, 55, 14]
        assert all(row.error is None for row in history)
        assert all(row.started_at <= row.completed_at for row in history)
        assert [row.completed_at for row in history] == sorted(row.completed_at for row in history)
        rows = await incident_rows()
        assert len(rows) == 2
        assert [row.check_id for row in rows] == [history[1].id, history[3].id]
        assert [row.occurred_at for row in rows] == [
            history[1].completed_at,
            history[3].completed_at,
        ]
        assert [(row.endpoint_id, row.user_id, row.healthy, row.state) for row in rows] == [
            (endpoint.id, 101, False, "sent"),
            (endpoint.id, 101, True, "sent"),
        ]

        deliveries = telegram.send_message.await_args_list
        assert len(deliveries) == 2
        assert sum(" is DOWN." in item.kwargs["text"] for item in deliveries) == 1
        # The existing user-facing recovery notification is labeled UP.
        assert sum(" is UP." in item.kwargs["text"] for item in deliveries) == 1
        assert all(item.kwargs["chat_id"] == 101 for item in deliveries)
        for delivery, incident in zip(deliveries, rows, strict=True):
            assert f"Endpoint #{endpoint.id}" in delivery.kwargs["text"]
            assert f"Incident #{incident.id} " in delivery.kwargs["text"]

        # A second owner's independent baseline/history never generates these alerts.
        assert [row.status for row in await repository.history(202, other.id)] == [200] * 4
        assert await repository.get(202, endpoint.id) is None
        assert await repository.history(202, endpoint.id) == []
        assert not await repository.remove(202, endpoint.id)
        assert [row.id for row in await repository.list(202)] == [other.id]

        # Verify ownership at the Telegram command boundary too, without a token or API call.
        calls_before = checker.check.await_count
        for action in ("check", "history", "remove"):
            message = SimpleNamespace(
                chat=SimpleNamespace(type="private"),
                from_user=SimpleNamespace(id=202),
                answer=AsyncMock(),
            )
            await endpoint_command(
                message, CommandObject(command=action, args=str(endpoint.id)), repository, monitor
            )
            message.answer.assert_awaited_once_with("Endpoint not found.")
        assert checker.check.await_count == calls_before == 8
        assert checker.check.await_args_list == [call(target_url), call(other_url)] * 4
        assert (await repository.get(101, endpoint.id)).url == target_url
        assert len(await repository.history(101, endpoint.id)) == 4
        assert telegram.send_message.await_count == 2
    finally:
        await monitor.stop()
        await repository.close()
