"""Safe download of an image from a user-supplied URL (server-side request forgery protection).

The dashboard server fetches the URL, so it must never be tricked into reaching internal
services (Railway's private network, cloud metadata, localhost). Protections:

* http/https only, default ports only (80/443), no credentials in the URL
* every address the host resolves to is checked, and the connection is made to exactly the
  addresses that were checked (a custom resolver), so DNS rebinding can't swap them afterwards
* IP-literal hosts are checked directly
* redirects are followed manually (max 4) and every hop goes through the same checks
* hard size cap and total timeout; the caller verifies the bytes really are an image
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

MAX_REDIRECTS = 4
TOTAL_TIMEOUT = 15
ALLOWED_PORTS = {None, 80, 443}


class FetchError(Exception):
    """Message is safe to show to the dashboard user."""


def is_blocked_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    return not addr.is_global or addr.is_multicast


class _SafeResolver(AbstractResolver):
    """Resolves normally, but refuses hosts that point at any non-public address."""

    def __init__(self, allow_private: bool = False):
        self._allow_private = allow_private

    async def resolve(self, host, port=0, family=socket.AF_INET):
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM)
        except socket.gaierror:
            raise FetchError("Couldn't find that website.") from None
        out = []
        for fam, _t, _p, _c, sockaddr in infos:
            ip = sockaddr[0]
            if not self._allow_private and is_blocked_ip(ip):
                raise FetchError("That address isn't allowed (it points to a private or internal network).")
            out.append({"hostname": host, "host": ip, "port": port, "family": fam, "proto": 0, "flags": socket.AI_NUMERICHOST})
        if not out:
            raise FetchError("Couldn't find that website.")
        return out

    async def close(self):
        return None


def check_url(url: str, allow_private: bool = False) -> str:
    url = (url or "").strip()
    if not url or len(url) > 2000:
        raise FetchError("Paste a valid image link.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise FetchError("Paste a valid image link.") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("The link must start with http:// or https://")
    if parts.username or parts.password:
        raise FetchError("Links containing a username or password aren't allowed.")
    if port not in ALLOWED_PORTS and not allow_private:
        raise FetchError("Only standard web ports (80/443) are allowed.")
    host = parts.hostname
    try:
        ipaddress.ip_address(host)
        is_literal = True
    except ValueError:
        is_literal = False
    if is_literal and not allow_private and is_blocked_ip(host):
        raise FetchError("That address isn't allowed (it points to a private or internal network).")
    return url


async def fetch_image_bytes(url: str, max_bytes: int, *, _allow_private: bool = False) -> bytes:
    """Download `url` (max `max_bytes`). `_allow_private` exists only for tests."""
    url = check_url(url, _allow_private)
    connector = aiohttp.TCPConnector(resolver=_SafeResolver(_allow_private), use_dns_cache=False, limit=2)
    timeout = aiohttp.ClientTimeout(total=TOTAL_TIMEOUT)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ServerIconFetcher)", "Accept": "image/*"}
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=timeout, headers=headers, trust_env=False) as session:
            for _ in range(MAX_REDIRECTS + 1):
                async with session.get(url, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("Location")
                        if not loc:
                            raise FetchError("The link redirected without a destination.")
                        url = check_url(urljoin(url, loc), _allow_private)
                        continue
                    if resp.status != 200:
                        raise FetchError(f"That website answered with an error ({resp.status}).")
                    if (resp.content_length or 0) > max_bytes:
                        raise FetchError(f"That image is too large (max {max_bytes // (1024 * 1024)} MB).")
                    chunks, size = [], 0
                    async for chunk in resp.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > max_bytes:
                            raise FetchError(f"That image is too large (max {max_bytes // (1024 * 1024)} MB).")
                        chunks.append(chunk)
                    return b"".join(chunks)
            raise FetchError("The link redirected too many times.")
    except FetchError:
        raise
    except asyncio.TimeoutError:
        raise FetchError("That website took too long to respond.") from None
    except aiohttp.ClientConnectorError as exc:
        # the resolver's FetchError is wrapped by aiohttp; surface it cleanly
        cause = exc.__cause__ or exc.os_error
        if isinstance(cause, FetchError):
            raise cause from None
        raise FetchError("Couldn't connect to that website.") from None
    except aiohttp.ClientError:
        raise FetchError("Couldn't download that link.") from None
