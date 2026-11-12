import socket
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import TCPConnector

from app.security import PublicResolver, UnsafeURL, public_socket, validate_url


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://localhost",
        "https://foo.localhost",
        "https://127.0.0.1",
        "https://10.1.2.3",
        "https://169.254.169.254/latest",
        "https://192.168.1.1",
        "https://100.64.0.1",
        "https://0.0.0.0",
        "https://224.0.0.1",
        "https://192.0.2.1",
        "https://[::1]",
        "https://[fc00::1]",
        "https://[fe80::1]",
        "https://[::ffff:8.8.8.8]",
        "https://user:pass@example.com",
        "https://@example.com",
        "https://example.com:8443",
        "https://2130706433",
        "https://0x7f000001",
        "https://127.1",
        "https://example.com\\@localhost",
        "https://example.com\n",
        "https://foo.internal",
        "https://[fe80::1%25eth0]",
        "https://",
        "https://example.com:bad",
    ],
)
def test_reject_unsafe_urls(url):
    with pytest.raises(UnsafeURL):
        validate_url(url)


@pytest.mark.parametrize(
    "url", ["https://example.com/health?q=1", "https://8.8.8.8", "https://[2606:4700:4700::1111]"]
)
def test_accept_public_urls(url):
    assert str(validate_url(url)) == url


async def test_resolver_checks_every_answer_and_every_call():
    delegate = AsyncMock()
    delegate.resolve.side_effect = [
        [{"host": "8.8.8.8"}],
        [{"host": "8.8.8.8"}, {"host": "127.0.0.1"}],
    ]
    resolver = PublicResolver(delegate)
    assert await resolver.resolve("example.com") == [{"host": "8.8.8.8"}]
    with pytest.raises(UnsafeURL):
        await resolver.resolve("example.com")
    await resolver.close()
    delegate.close.assert_awaited_once()


async def test_empty_dns():
    delegate = AsyncMock()
    delegate.resolve.return_value = []
    with pytest.raises(UnsafeURL):
        await PublicResolver(delegate).resolve("example.com")


def test_socket_blocks_private_before_creation(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(socket, "socket", factory)
    with pytest.raises(UnsafeURL):
        public_socket((socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443)))
    factory.assert_not_called()


def test_socket_uses_validated_address_family(monkeypatch):
    factory = Mock()
    monkeypatch.setattr(socket, "socket", factory)
    public_socket((socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)))
    factory.assert_called_once_with(family=socket.AF_INET, type=socket.SOCK_STREAM, proto=6)


async def test_real_connector_revalidates_dns_without_cache():
    delegate = AsyncMock()

    def record(ip):
        return {
            "hostname": "example.com",
            "host": ip,
            "port": 443,
            "family": socket.AF_INET,
            "proto": 0,
            "flags": 0,
        }

    delegate.resolve.side_effect = [[record("8.8.8.8")], [record("10.0.0.1")]]
    resolver = PublicResolver(delegate)
    async with TCPConnector(
        resolver=resolver, use_dns_cache=False, force_close=True, socket_factory=public_socket
    ) as connector:
        assert (await connector._resolve_host("example.com", 443))[0]["host"] == "8.8.8.8"
        with pytest.raises(UnsafeURL):
            await connector._resolve_host("example.com", 443)
    await resolver.close()
    assert delegate.resolve.await_count == 2
