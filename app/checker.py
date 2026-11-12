import asyncio
import ssl
from dataclasses import dataclass
from time import perf_counter

import aiohttp
import certifi
from yarl import URL

from app.security import PublicResolver, UnsafeURL, public_socket, validate_url


@dataclass(frozen=True)
class CheckResult:
    status: int | None
    latency_ms: float
    error: str | None = None

    @property
    def healthy(self) -> bool:
        return self.status is not None and 200 <= self.status < 400 and self.error is None


class HealthChecker:
    def __init__(self, timeout: float = 10, max_redirects: int = 3, concurrency: int = 10):
        self.timeout = timeout
        self.max_redirects = max_redirects
        self.semaphore = asyncio.Semaphore(concurrency)
        # Keep system/enterprise roots and supplement incomplete Windows CA stores.
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.load_verify_locations(cafile=certifi.where())

    async def check(self, value: str) -> CheckResult:
        async with self.semaphore:
            started = perf_counter()
            try:
                # One deadline includes DNS, TLS and every redirect hop.
                async with asyncio.timeout(self.timeout):
                    url = validate_url(value)
                    resolver = PublicResolver()
                    try:
                        connector = aiohttp.TCPConnector(
                            resolver=resolver,
                            use_dns_cache=False,
                            force_close=True,
                            socket_factory=public_socket,
                            ssl=self.ssl_context,
                        )
                        async with aiohttp.ClientSession(
                            connector=connector,
                            trust_env=False,
                            cookie_jar=aiohttp.DummyCookieJar(),
                            timeout=aiohttp.ClientTimeout(total=self.timeout),
                            headers={"User-Agent": "PULSEBOT/0.1"},
                        ) as session:
                            for hop in range(self.max_redirects + 1):
                                async with session.get(url, allow_redirects=False) as response:
                                    if response.status in {301, 302, 303, 307, 308}:
                                        location = response.headers.get("Location")
                                        if not location:
                                            raise UnsafeURL("The redirect has no destination.")
                                        if hop == self.max_redirects:
                                            raise UnsafeURL("Too many redirects.")
                                        # Reject controls before URL parsing can strip them.
                                        if any(ord(c) <= 32 or ord(c) == 127 for c in location):
                                            raise UnsafeURL("The redirect destination is invalid.")
                                        url = validate_url(str(url.join(URL(location))))
                                        continue
                                    return CheckResult(
                                        response.status, (perf_counter() - started) * 1000
                                    )
                    finally:
                        await resolver.close()
            except UnsafeURL as exc:
                error = str(exc)
            except TimeoutError:
                error = "The endpoint timed out."
            except (aiohttp.ClientError, OSError, ValueError):
                # Never echo exception strings: URLs or infrastructure data may be sensitive.
                error = "Could not connect securely. Check the hostname and TLS certificate."
            return CheckResult(None, (perf_counter() - started) * 1000, error)
