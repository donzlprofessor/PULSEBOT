import pytest
from sqlalchemy.exc import IntegrityError

from app.db import Repository


async def test_ownership_and_duplicates(repository):
    endpoint = await repository.add(10, "https://example.com")
    assert await repository.get(20, endpoint.id) is None
    assert await repository.list(20) == []
    assert not await repository.remove(20, endpoint.id)
    assert (await repository.get(10, endpoint.id)).url == endpoint.url
    with pytest.raises(IntegrityError):
        await repository.add(10, endpoint.url)
    await repository.add(20, endpoint.url)
    assert await repository.remove(10, endpoint.id)
    assert not await repository.remove(10, endpoint.id)


async def test_persistence(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'persistent.db'}"
    first = Repository(url)
    await first.initialize()
    endpoint = await first.add(42, "https://example.com")
    await first.close()
    second = Repository(url)
    try:
        assert (await second.get(42, endpoint.id)).url == endpoint.url
    finally:
        await second.close()
