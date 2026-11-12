"""URL policy plus validation at the actual DNS and socket connection boundaries."""

import ipaddress
import re
import socket
from urllib.parse import urlsplit

from aiohttp.abc import AbstractResolver
from aiohttp.resolver import DefaultResolver
from yarl import URL


class UnsafeURL(ValueError):
    pass


def require_public_ip(address: str) -> None:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        raise UnsafeURL("The destination did not resolve to a valid IP address.") from None
    # Reject transition mechanisms as well as mapped IPv4 to avoid ambiguous routing.
    if (
        not ip.is_global
        or ip.is_multicast
        or ip.is_reserved
        or (isinstance(ip, ipaddress.IPv6Address) and (ip.ipv4_mapped or ip.sixtofour or ip.teredo))
    ):
        raise UnsafeURL("Only public Internet destinations are allowed.")


def validate_url(value: str) -> URL:
    if not value or len(value) > 2048 or any(ord(c) <= 32 or ord(c) == 127 for c in value):
        raise UnsafeURL("Provide one HTTPS URL without spaces (maximum 2048 characters).")
    if "\\" in value:
        raise UnsafeURL("Backslashes are not allowed in URLs.")
    try:
        if "@" in urlsplit(value).netloc:
            raise UnsafeURL("URLs containing credentials are not allowed.")
        url = URL(value)
        host = url.host
        port = url.port
    except UnsafeURL:
        raise
    except (ValueError, UnicodeError):
        raise UnsafeURL("That URL is invalid.") from None
    if url.scheme != "https" or not host or port != 443:
        raise UnsafeURL("Use a public HTTPS URL on port 443.")
    if url.user is not None or url.password is not None:
        raise UnsafeURL("URLs containing credentials are not allowed.")
    if "%" in host:
        raise UnsafeURL("Scoped or encoded host addresses are not allowed.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        name = url.raw_host.rstrip(".").lower()
        labels = name.split(".")
        if (
            len(labels) < 2
            or name.endswith((".localhost", ".local", ".internal", ".test", ".invalid"))
            or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", x) for x in labels)
            or not re.fullmatch(r"[a-z]{2,63}|xn--[a-z0-9-]+", labels[-1])
        ):
            raise UnsafeURL("Use a public Internet hostname.") from None
    else:
        require_public_ip(host)
    return url.with_fragment(None)


class PublicResolver(AbstractResolver):
    def __init__(self, delegate: AbstractResolver | None = None):
        self.delegate = delegate if delegate is not None else DefaultResolver()

    async def resolve(self, host, port=0, family=socket.AF_INET):
        records = await self.delegate.resolve(host, port, family)
        if not records:
            raise UnsafeURL("The hostname has no usable addresses.")
        for record in records:
            require_public_ip(record["host"])
        return records

    async def close(self):
        await self.delegate.close()


def public_socket(addr_info):
    """Validate the exact numeric address used by aiohttp, including literal IP URLs."""
    family, kind, proto, _, address = addr_info
    require_public_ip(address[0])
    return socket.socket(family=family, type=kind, proto=proto)
