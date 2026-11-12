"""Shared manual/scheduled check coordination and durable transition outbox."""

import asyncio
import logging
from datetime import UTC, datetime

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from sqlalchemy.exc import SQLAlchemyError

from app.checker import CheckResult, HealthChecker
from app.db import Endpoint, Repository
from app.instance import InstanceLock

logger = logging.getLogger(__name__)


class CheckInProgress(Exception):
    pass


class MonitoringService:
    def __init__(
        self,
        repository: Repository,
        checker: HealthChecker,
        bot: Bot | None,
        interval: float = 60,
        concurrency: int = 10,
    ):
        self.repository = repository
        self.checker = checker
        self.bot = bot
        self.interval = interval
        self.concurrency = concurrency
        self.slots = asyncio.Semaphore(concurrency)
        self.busy: set[int] = set()
        self.active: set[asyncio.Task] = set()
        self.stopping = False
        self.tasks: list[asyncio.Task] = []
        self.instance = InstanceLock(repository.engine.url.database)

    async def start(self) -> None:
        if self.tasks:
            raise RuntimeError("Monitoring is already running.")
        self.instance.acquire()
        try:
            await self.repository.initialize()
        except BaseException:
            self.instance.release()
            raise
        self.stopping = False
        self.tasks = [asyncio.create_task(self._schedule()), asyncio.create_task(self._alerts())]

    async def stop(self) -> None:
        self.stopping = True
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        active = list(self.active)
        for task in active:
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        self.tasks.clear()
        self.instance.release()

    async def check(self, endpoint: Endpoint) -> CheckResult:
        if self.stopping or endpoint.id in self.busy:
            raise CheckInProgress
        self.busy.add(endpoint.id)
        task = asyncio.current_task()
        self.active.add(task)
        try:
            async with self.slots:
                started_at = datetime.now(UTC)
                result = await self.checker.check(endpoint.url)
                await self.repository.record(endpoint, result, started_at)
                return result
        finally:
            self.busy.discard(endpoint.id)
            self.active.discard(task)

    async def run_cycle(self) -> None:
        # Page through endpoints, with a fixed number of workers rather than a task per URL.
        after = 0
        while not self.stopping:
            endpoints = await self.repository.page(after, self.concurrency)
            if not endpoints:
                return
            after = endpoints[-1].id

            async def check_one(endpoint):
                try:
                    await self.check(endpoint)
                except CheckInProgress:
                    pass
                except Exception:
                    logger.error(
                        "A scheduled check failed internally; it will be retried next cycle."
                    )

            async with asyncio.TaskGroup() as group:
                for endpoint in endpoints:
                    group.create_task(check_one(endpoint))

    async def _schedule(self) -> None:
        while True:
            started = asyncio.get_running_loop().time()
            try:
                await self.run_cycle()
            except Exception:
                logger.error("Monitoring cycle failed; retrying after the interval.")
            elapsed = asyncio.get_running_loop().time() - started
            # Never overlap cycles or replay missed ticks after a slow cycle.
            await asyncio.sleep(max(self.interval - elapsed, 1))

    async def deliver_one(self) -> bool:
        if self.bot is None:
            return False
        incident = await self.repository.claim_incident()
        if incident is None:
            return False
        state = "UP" if incident.healthy else "DOWN"
        try:
            async with asyncio.timeout(15):
                await self.bot.send_message(
                    chat_id=incident.user_id,
                    text=f"Endpoint #{incident.endpoint_id} is {state}.\n"
                    f"Incident #{incident.id} at {incident.occurred_at.isoformat()} UTC",
                )
        except TelegramRetryAfter as exc:
            # Telegram explicitly rejected this attempt, so retrying cannot duplicate it.
            await self.repository.finish_incident(incident.id, "pending")
            await asyncio.sleep(max(exc.retry_after, 1))
        except Exception:
            await self.repository.finish_incident(incident.id, "uncertain")
            logger.error("Incident delivery failed; not resending an uncertain Telegram delivery.")
        else:
            await self.repository.finish_incident(incident.id, "sent")
        return True

    async def _alerts(self) -> None:
        while True:
            try:
                if await self.deliver_one():
                    continue
            except SQLAlchemyError:
                logger.error("Alert storage unavailable; retrying shortly.")
            await asyncio.sleep(1)
