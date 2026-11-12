import asyncio
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy import func, select, text

from app.checker import CheckResult
from app.db import CheckRecord, Incident, Repository
from app.instance import InstanceLock
from app.monitor import CheckInProgress, MonitoringService

UP = CheckResult(200, 25)
DOWN = CheckResult(503, 75)


def service(repository, results=None, **kwargs):
    checker = AsyncMock()
    checker.check.side_effect = results
    checker.check.return_value = UP
    bot = AsyncMock()
    return MonitoringService(repository, checker, bot, **kwargs)


async def incidents(repository):
    async with repository.sessions() as session:
        return list(await session.scalars(select(Incident).order_by(Incident.id)))


async def test_transitions_persist_and_alert_once(repository):
    endpoint = await repository.add(10, "https://example.com")
    monitor = service(repository, [UP, UP, DOWN, DOWN, UP, UP])
    for _ in range(6):
        await monitor.check(endpoint)
    history = await repository.history(10, endpoint.id)
    assert len(history) == 6
    assert history[0].latency_ms == 25
    assert history[0].status == 200
    assert history[0].started_at <= history[0].completed_at
    assert [row.healthy for row in await incidents(repository)] == [False, True]
    assert await monitor.deliver_one()
    assert await monitor.deliver_one()
    assert not await monitor.deliver_one()
    assert monitor.bot.send_message.await_count == 2
    messages = monitor.bot.send_message.call_args_list
    assert messages[0].kwargs["chat_id"] == 10
    assert "DOWN" in messages[0].kwargs["text"]
    assert "UP" in messages[1].kwargs["text"]
    assert [row.state for row in await incidents(repository)] == ["sent", "sent"]


async def test_initial_failure_is_baseline_and_error_is_saved(repository):
    endpoint = await repository.add(10, "https://example.com")
    monitor = service(repository, [CheckResult(None, 100, "The endpoint timed out."), UP])
    await monitor.check(endpoint)
    assert await incidents(repository) == []
    row = (await repository.history(10, endpoint.id))[0]
    assert row.status is None
    assert row.error == "The endpoint timed out."
    assert not row.healthy
    await monitor.check(endpoint)
    assert len(await incidents(repository)) == 1


async def test_restart_preserves_baseline_history_and_pending_alert(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'restart.db'}"
    repo = Repository(url)
    await repo.initialize()
    endpoint = await repo.add(10, "https://example.com")
    monitor = service(repo, [UP, DOWN])
    await monitor.check(endpoint)
    await monitor.check(endpoint)
    await repo.close()
    repo = Repository(url)
    try:
        await repo.initialize()
        monitor = service(repo, [DOWN, UP])
        await monitor.check(endpoint)
        assert len(await incidents(repo)) == 1
        assert await monitor.deliver_one()
        await monitor.check(endpoint)
        assert await monitor.deliver_one()
        assert not await monitor.deliver_one()
        assert len(await repo.history(10, endpoint.id)) == 4
    finally:
        await repo.close()


async def test_no_overlapping_manual_and_scheduled_check(repository):
    endpoint = await repository.add(10, "https://example.com")
    monitor = service(repository)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow(url):
        entered.set()
        await release.wait()
        return UP

    monitor.checker.check.side_effect = slow
    task = asyncio.create_task(monitor.check(endpoint))
    await entered.wait()
    try:
        with pytest.raises(CheckInProgress):
            await monitor.check(endpoint)
        await monitor.run_cycle()
        assert monitor.checker.check.await_count == 1
    finally:
        release.set()
        await task
    assert len(await repository.history(10, endpoint.id)) == 1


async def test_bounded_workers_and_isolated_failure(repository):
    for i in range(7):
        await repository.add(10, f"https://example.com/{i}")
    monitor = service(repository, concurrency=2)
    running = peak = 0

    async def check(url):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        try:
            await asyncio.sleep(0.001)
            if url.endswith("/0"):
                raise RuntimeError("test internal failure")
            return UP
        finally:
            running -= 1

    monitor.checker.check.side_effect = check
    await monitor.run_cycle()
    assert peak == 2
    assert not monitor.busy
    async with repository.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(CheckRecord)) == 6


async def test_deleted_endpoint_cannot_gain_history_or_alert(repository):
    endpoint = await repository.add(10, "https://example.com")
    await repository.remove(10, endpoint.id)
    assert await repository.record(endpoint, DOWN, datetime.now(UTC)) is None
    assert await incidents(repository) == []


async def test_remove_cleans_history_and_outbox(repository):
    endpoint = await repository.add(10, "https://example.com")
    monitor = service(repository, [UP, DOWN])
    await monitor.check(endpoint)
    await monitor.check(endpoint)
    assert not await repository.remove(20, endpoint.id)
    assert len(await incidents(repository)) == 1
    assert await repository.remove(10, endpoint.id)
    assert await repository.history(10, endpoint.id) == []
    assert await incidents(repository) == []


async def test_ambiguous_delivery_is_not_retried(repository):
    endpoint = await repository.add(10, "https://example.com")
    monitor = service(repository, [UP, DOWN])
    await monitor.check(endpoint)
    await monitor.check(endpoint)
    monitor.bot.send_message.side_effect = TimeoutError()
    assert await monitor.deliver_one()
    assert not await monitor.deliver_one()
    assert (await incidents(repository))[0].state == "uncertain"
    restarted = service(repository)
    assert not await restarted.deliver_one()
    monitor.bot.send_message.assert_awaited_once()


async def test_claim_is_atomic_and_survives_interrupted_delivery(repository):
    endpoint = await repository.add(10, "https://example.com")
    monitor = service(repository, [UP, DOWN])
    await monitor.check(endpoint)
    await monitor.check(endpoint)
    claims = await asyncio.gather(repository.claim_incident(), repository.claim_incident())
    assert sum(row is not None for row in claims) == 1
    assert not await service(repository).deliver_one()


async def test_explicit_rate_limit_retries_safely(repository, monkeypatch):
    endpoint = await repository.add(10, "https://example.com")
    monitor = service(repository, [UP, DOWN])
    await monitor.check(endpoint)
    await monitor.check(endpoint)
    monitor.bot.send_message.side_effect = [
        TelegramRetryAfter(
            method=SendMessage(chat_id=10, text="test"), message="Rate limited", retry_after=1
        ),
        None,
    ]
    monkeypatch.setattr("app.monitor.asyncio.sleep", AsyncMock())
    await monitor.deliver_one()
    assert (await incidents(repository))[0].state == "pending"
    await monitor.deliver_one()
    assert (await incidents(repository))[0].state == "sent"


async def test_duplicate_scheduler_and_graceful_stop(repository):
    first = service(repository)
    second = service(repository)
    await first.start()
    try:
        with pytest.raises(RuntimeError, match="already"):
            await first.start()
        with pytest.raises(RuntimeError, match="already"):
            await second.start()
    finally:
        await first.stop()
    await second.start()
    await second.stop()
    assert not first.tasks
    assert first.instance.file is None


async def test_stop_cancels_inflight_manual_check_and_cleans_busy(repository):
    endpoint = await repository.add(10, "https://example.com")
    monitor = service(repository)
    entered = asyncio.Event()

    async def slow(url):
        entered.set()
        await asyncio.Event().wait()

    monitor.checker.check.side_effect = slow
    task = asyncio.create_task(monitor.check(endpoint))
    await entered.wait()
    await monitor.stop()
    assert task.cancelled()
    assert not monitor.busy
    assert not monitor.active
    with pytest.raises(CheckInProgress):
        await monitor.check(endpoint)


async def test_scheduler_runs_immediately_and_waits_without_overlap(repository, monkeypatch):
    monitor = service(repository, interval=60)
    monitor.run_cycle = AsyncMock()
    sleep = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr("app.monitor.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await monitor._schedule()
    monitor.run_cycle.assert_awaited_once()
    assert 0 < sleep.call_args.args[0] <= 60


async def test_phase1_database_is_upgraded_without_endpoint_changes(tmp_path):
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path / 'phase1.db'}")
    try:
        async with repo.engine.begin() as connection:
            await connection.execute(
                text(
                    "CREATE TABLE endpoints (id INTEGER PRIMARY KEY, user_id BIGINT NOT NULL, "
                    "url VARCHAR(2048) NOT NULL, created_at DATETIME NOT NULL, "
                    "UNIQUE(user_id, url))"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO endpoints VALUES "
                    "(1, 10, 'https://example.com', '2026-01-01 00:00:00')"
                )
            )
        await repo.initialize()
        endpoint = await repo.get(10, 1)
        assert endpoint.url == "https://example.com"
        await service(repo).check(endpoint)
        assert len(await repo.history(10, 1)) == 1
    finally:
        await repo.close()


def test_instance_lock_rejects_memory_database():
    with pytest.raises(ValueError):
        InstanceLock(":memory:")


def test_lock_excludes_another_process_and_releases(tmp_path):
    database = str(tmp_path / "lock.db")
    lock = InstanceLock(database)
    code = (
        "import sys\nfrom app.instance import InstanceLock\n"
        "lock = InstanceLock(sys.argv[1])\n"
        "try:\n    lock.acquire()\n"
        "except RuntimeError:\n    sys.exit(23)\n"
        "else:\n    lock.release()\n"
    )

    def probe():
        return subprocess.run(
            [sys.executable, "-c", code, database],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            timeout=10,
            check=False,
        ).returncode

    lock.acquire()
    try:
        assert probe() == 23
    finally:
        lock.release()
    assert probe() == 0


async def test_concurrent_state_writes_create_only_one_transition(repository):
    endpoint = await repository.add(10, "https://example.com")
    await repository.record(endpoint, UP, datetime.now(UTC))
    await asyncio.gather(*(repository.record(endpoint, DOWN, datetime.now(UTC)) for _ in range(3)))
    assert len(await repository.history(10, endpoint.id)) == 4
    assert len(await incidents(repository)) == 1


async def test_startup_failure_releases_instance_lock(repository, monkeypatch):
    monitor = service(repository)
    monkeypatch.setattr(repository, "initialize", AsyncMock(side_effect=RuntimeError("test")))
    with pytest.raises(RuntimeError, match="test"):
        await monitor.start()
    assert not monitor.tasks
    replacement = InstanceLock(repository.engine.url.database)
    replacement.acquire()
    replacement.release()
