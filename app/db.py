from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    String,
    UniqueConstraint,
    delete,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.checker import CheckResult


class Base(DeclarativeBase):
    pass


class Endpoint(Base):
    __tablename__ = "endpoints"
    __table_args__ = (UniqueConstraint("user_id", "url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    url: Mapped[str] = mapped_column(String(2048))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class Repository:
    def __init__(self, database_url: str):
        self.engine = create_async_engine(database_url)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def close(self) -> None:
        await self.engine.dispose()

    async def add(self, user_id: int, url: str) -> Endpoint:
        async with self.sessions.begin() as session:
            endpoint = Endpoint(user_id=user_id, url=url)
            session.add(endpoint)
        return endpoint

    async def list(self, user_id: int) -> list[Endpoint]:
        async with self.sessions() as session:
            result = await session.scalars(
                select(Endpoint).where(Endpoint.user_id == user_id).order_by(Endpoint.id)
            )
            return list(result)

    async def get(self, user_id: int, endpoint_id: int) -> Endpoint | None:
        async with self.sessions() as session:
            return await session.scalar(
                select(Endpoint).where(Endpoint.user_id == user_id, Endpoint.id == endpoint_id)
            )

    async def remove(self, user_id: int, endpoint_id: int) -> bool:
        async with self.sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            endpoint = await session.scalar(
                select(Endpoint).where(Endpoint.user_id == user_id, Endpoint.id == endpoint_id)
            )
            if endpoint is None:
                return False
            await session.execute(delete(Incident).where(Incident.endpoint_id == endpoint_id))
            await session.execute(delete(CheckRecord).where(CheckRecord.endpoint_id == endpoint_id))
            result = await session.execute(
                delete(Endpoint).where(Endpoint.user_id == user_id, Endpoint.id == endpoint_id)
            )
            return result.rowcount > 0

    async def page(self, after: int = 0, limit: int = 100) -> list[Endpoint]:
        async with self.sessions() as session:
            return list(
                await session.scalars(
                    select(Endpoint).where(Endpoint.id > after).order_by(Endpoint.id).limit(limit)
                )
            )

    async def history(self, user_id: int, endpoint_id: int, limit: int = 10) -> list[CheckRecord]:
        async with self.sessions() as session:
            return list(
                await session.scalars(
                    select(CheckRecord)
                    .join(Endpoint, Endpoint.id == CheckRecord.endpoint_id)
                    .where(Endpoint.user_id == user_id, Endpoint.id == endpoint_id)
                    .order_by(CheckRecord.id.desc())
                    .limit(limit)
                )
            )

    async def record(
        self, endpoint: Endpoint, result: CheckResult, started_at: datetime
    ) -> CheckRecord | None:
        async with self.sessions.begin() as session:
            # Serialize read/compare/write so state and its outbox event commit atomically.
            await session.execute(text("BEGIN IMMEDIATE"))
            exists = await session.scalar(
                select(Endpoint).where(
                    Endpoint.id == endpoint.id,
                    Endpoint.user_id == endpoint.user_id,
                    Endpoint.url == endpoint.url,
                )
            )
            if exists is None:
                return None
            previous = await session.scalar(
                select(CheckRecord)
                .where(CheckRecord.endpoint_id == endpoint.id)
                .order_by(CheckRecord.id.desc())
                .limit(1)
            )
            record = CheckRecord(
                endpoint_id=endpoint.id,
                healthy=result.healthy,
                status=result.status,
                latency_ms=result.latency_ms,
                error=result.error,
                started_at=started_at,
                completed_at=datetime.now(UTC),
            )
            session.add(record)
            await session.flush()
            # The first observation establishes a baseline; it is not a transition.
            if previous is not None and previous.healthy != record.healthy:
                session.add(
                    Incident(
                        endpoint_id=endpoint.id,
                        user_id=endpoint.user_id,
                        check_id=record.id,
                        healthy=record.healthy,
                        state="pending",
                        occurred_at=record.completed_at,
                    )
                )
            return record

    async def claim_incident(self) -> Incident | None:
        async with self.sessions.begin() as session:
            await session.execute(text("BEGIN IMMEDIATE"))
            incident = await session.scalar(
                select(Incident).where(Incident.state == "pending").order_by(Incident.id).limit(1)
            )
            if incident is not None:
                # Claim before Telegram I/O. Uncertain deliveries are never automatically resent.
                incident.state = "attempted"
            return incident

    async def finish_incident(self, incident_id: int, state: str) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                update(Incident).where(Incident.id == incident_id).values(state=state)
            )


class CheckRecord(Base):
    __tablename__ = "checks"
    id: Mapped[int] = mapped_column(primary_key=True)
    endpoint_id: Mapped[int] = mapped_column(index=True)
    healthy: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[int | None]
    latency_ms: Mapped[float] = mapped_column(Float)
    error: Mapped[str | None] = mapped_column(String(512))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[int] = mapped_column(primary_key=True)
    endpoint_id: Mapped[int] = mapped_column(index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    check_id: Mapped[int] = mapped_column(unique=True)
    healthy: Mapped[bool] = mapped_column(Boolean)
    state: Mapped[str] = mapped_column(String(20), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
