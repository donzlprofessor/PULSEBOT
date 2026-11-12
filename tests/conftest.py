import socket

import pytest_asyncio

from app.db import Repository


@pytest_asyncio.fixture(autouse=True)
async def no_network(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("Tests must not access the network")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


@pytest_asyncio.fixture
async def repository(tmp_path):
    repo = Repository(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await repo.initialize()
    yield repo
    await repo.close()
