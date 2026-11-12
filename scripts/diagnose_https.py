r"""Fixed-target diagnostics. Never loads settings, .env, Telegram, or the database.

Run from the project root: .venv\Scripts\python.exe -m scripts.diagnose_https
"""

import asyncio
import ssl

import aiohttp
import certifi

from app.checker import HealthChecker
from app.security import PublicResolver, public_socket, validate_url

TARGET = "https://example.com"


def report_error(exc: BaseException) -> None:
    """Print only allowlisted diagnostic fields, never arbitrary exception text."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        print(f"Exception type: {type(exc).__name__}")
        if isinstance(exc, OSError):
            print(f"OS errno: {exc.errno}; Windows error: {getattr(exc, 'winerror', None)}")
        if isinstance(exc, ssl.SSLCertVerificationError):
            print(f"Certificate verification code: {getattr(exc, 'verify_code', None)}")
        exc = exc.__cause__ or exc.__context__


async def main() -> None:
    print(f"Fixed target: {TARGET}; aiohttp {aiohttp.__version__}; {ssl.OPENSSL_VERSION}")
    print("TLS verification enabled; environment proxies disabled; SSRF checks enabled.")
    url = validate_url(TARGET)
    resolver = PublicResolver()
    try:
        try:
            records = await resolver.resolve(url.host, 443)
            print(f"DNS: {len(records)} public address(es) accepted.")
        except Exception as exc:
            print("DNS stage failed:")
            report_error(exc)
        # No automatic redirects: a diagnostic must never bypass destination validation.
        augmented = ssl.create_default_context()
        augmented.load_verify_locations(cafile=certifi.where())
        for name, context in [("system defaults", True), ("system + certifi", augmented)]:
            print(f"Trust configuration: {name}")
            async with aiohttp.TCPConnector(
                resolver=resolver,
                use_dns_cache=False,
                force_close=True,
                socket_factory=public_socket,
                ssl=context,
            ) as connector:
                async with aiohttp.ClientSession(
                    connector=connector, trust_env=False, timeout=aiohttp.ClientTimeout(total=10)
                ) as session:
                    try:
                        async with session.get(url, allow_redirects=False) as response:
                            print(f"HTTPS response: {response.status}")
                    except Exception as exc:
                        print("HTTPS stage failed:")
                        report_error(exc)
    finally:
        await resolver.close()
    result = await HealthChecker().check(TARGET)
    print(f"Checker: status={result.status}; error={result.error}; ms={result.latency_ms:.0f}")


if __name__ == "__main__":
    asyncio.run(main())
