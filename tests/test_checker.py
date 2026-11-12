import asyncio
import ssl
from unittest.mock import AsyncMock, Mock

import aiohttp
import certifi
import pytest

from app.checker import HealthChecker
from app.security import PublicResolver, public_socket


@pytest.fixture
def http(monkeypatch):
    responses = []
    session = Mock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)

    def get(*args, **kwargs):
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        response = Mock(status=item[0], headers=item[1])
        context = Mock()
        context.__aenter__ = AsyncMock(return_value=response)
        context.__aexit__ = AsyncMock(return_value=False)
        return context

    session.get.side_effect = get
    connector = Mock()
    factory = Mock(return_value=session)
    monkeypatch.setattr("app.checker.aiohttp.ClientSession", factory)
    monkeypatch.setattr("app.checker.aiohttp.TCPConnector", connector)
    return responses, session, connector, factory


@pytest.mark.parametrize("status,healthy", [(200, True), (204, True), (404, False), (503, False)])
async def test_status_and_latency(http, status, healthy):
    responses, session, connector, factory = http
    responses.append((status, {}))
    result = await HealthChecker().check("https://example.com")
    assert result.status == status
    assert result.healthy is healthy
    assert result.latency_ms >= 0
    assert result.error is None
    session.get.assert_called_once()
    assert session.get.call_args.kwargs == {"allow_redirects": False}
    options = connector.call_args.kwargs
    assert options["use_dns_cache"] is False
    assert options["force_close"] is True
    assert options["socket_factory"] is public_socket
    assert isinstance(options["resolver"], PublicResolver)
    assert options["ssl"].verify_mode == ssl.CERT_REQUIRED
    assert options["ssl"].check_hostname is True
    assert factory.call_args.kwargs["trust_env"] is False


@pytest.mark.parametrize(
    "location",
    [
        "http://example.com",
        "https://127.0.0.1",
        "https://user:pw@example.com",
        "https://[::1]",
        "https://example.com:8080",
        "https://example.com\n",
    ],
)
async def test_unsafe_redirect_never_requested(http, location):
    responses, session, _, _ = http
    responses.append((302, {"Location": location}))
    result = await HealthChecker().check("https://example.com")
    assert result.error
    assert session.get.call_count == 1


async def test_relative_redirect(http):
    responses, session, _, _ = http
    responses.extend([(302, {"Location": "/health"}), (200, {})])
    assert (await HealthChecker().check("https://example.com")).healthy
    assert str(session.get.call_args.args[0]) == "https://example.com/health"


async def test_redirect_limit(http):
    responses, session, _, _ = http
    responses.extend([(302, {"Location": "/again"})] * 3)
    result = await HealthChecker(max_redirects=1).check("https://example.com")
    assert result.error == "Too many redirects."
    assert session.get.call_count == 2


async def test_missing_location(http):
    http[0].append((302, {}))
    assert (await HealthChecker().check("https://example.com")).error


@pytest.mark.parametrize("exception", [TimeoutError(), aiohttp.ClientError(), OSError()])
async def test_network_failures_are_safe_results(http, exception):
    http[0].append(exception)
    result = await HealthChecker().check("https://example.com")
    assert result.error
    assert result.status is None
    assert not result.healthy


async def test_total_deadline(http):
    async def stalled(*args):
        await asyncio.sleep(1)

    context = Mock()
    context.__aenter__ = stalled
    context.__aexit__ = AsyncMock()
    http[1].get.side_effect = None
    http[1].get.return_value = context
    result = await HealthChecker(timeout=0.01).check("https://example.com")
    assert result.error == "The endpoint timed out."


async def test_unsafe_input_never_opens_session(http):
    result = await HealthChecker().check("https://10.0.0.1")
    assert result.error
    http[3].assert_not_called()


async def test_real_http_connector_blocks_private_dns(monkeypatch):
    # Exercise the actual ClientSession/TCPConnector path; no fake HTTP response.
    delegate = AsyncMock()
    delegate.resolve.return_value = [{"host": "127.0.0.1"}]
    resolver = PublicResolver(delegate)
    monkeypatch.setattr("app.checker.PublicResolver", lambda: resolver)
    result = await HealthChecker().check("https://example.com")
    assert result.error == "Only public Internet destinations are allowed."
    assert result.status is None
    delegate.resolve.assert_awaited_once()
    delegate.close.assert_awaited_once()


def test_tls_supplements_system_trust_with_certifi(monkeypatch):
    context = Mock()
    defaults = Mock(return_value=context)
    monkeypatch.setattr("app.checker.ssl.create_default_context", defaults)
    checker = HealthChecker()
    defaults.assert_called_once_with()
    context.load_verify_locations.assert_called_once_with(cafile=certifi.where())
    assert checker.ssl_context is context


async def test_certificate_verification_failure_still_rejected(http):
    # A trusted CA bundle must never turn verification failures into successful checks.
    http[0].append(ssl.SSLCertVerificationError(1, "test certificate verification failure"))
    result = await HealthChecker().check("https://example.com")
    assert result.status is None
    assert result.error == "Could not connect securely. Check the hostname and TLS certificate."
    assert not result.healthy
