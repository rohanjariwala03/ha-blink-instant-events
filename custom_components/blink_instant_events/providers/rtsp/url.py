"""RTSP addresses: what is accepted, where the credentials go, and keeping them out of logs.

Credentials are stored apart from the address (in the config entry) and only joined for ffmpeg and Home
Assistant's stream. Anything ffmpeg prints is passed through `redact` first: ffmpeg repeats its input
address, password included, in many of its errors.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit

SCHEMES = {"rtsp", "rtsps"}
# What ffmpeg may open for an RTSP input: never file:, concat:, http: or anything else a crafted URL names.
PROTOCOLS = "rtsp,rtsps,rtp,srtp,udp,tcp,tls"
BAD = re.compile(r"[\s\x00-\x1f\x7f]")
MAX_LENGTH = 2048


class InvalidUrl(ValueError):
    """Not an RTSP address this integration will open; the message says why (no secrets in it)."""


def parse(url: str) -> tuple[str, str | None, str | None]:
    """(address without credentials, username, password) of an rtsp:// or rtsps:// address."""
    url = url.strip()
    if not url or len(url) > MAX_LENGTH or BAD.search(url):
        raise InvalidUrl("not_rtsp")
    parts = urlsplit(url)
    if parts.scheme.lower() not in SCHEMES:
        raise InvalidUrl("not_rtsp")
    try:
        host, port = parts.hostname, parts.port  # port raises on garbage
    except ValueError as err:
        raise InvalidUrl("bad_host") from err
    if not host or host.startswith("-"):
        raise InvalidUrl("bad_host")
    netloc = f"[{host}]" if _is_ipv6(host) else host
    if port is not None:
        netloc += f":{port}"
    clean = urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, ""))
    username = unquote(parts.username) if parts.username else None
    password = unquote(parts.password) if parts.password else None
    return clean, username, password


def with_credentials(url: str, username: str | None, password: str | None) -> str:
    """The address with the credentials in it, for ffmpeg and Home Assistant's stream only."""
    if not username:
        return url
    parts = urlsplit(url)
    userinfo = quote(username, safe="") + (f":{quote(password, safe='')}" if password else "")
    return urlunsplit((parts.scheme, f"{userinfo}@{parts.netloc}", parts.path, parts.query, ""))


def redact(text: str, password: str | None) -> str:
    """`text` without the password (raw or URL-encoded) or the user:password@ part of any address."""
    text = re.sub(r"(rtsps?://)[^/@\s]*@", r"\1***@", text)
    for secret in {password, quote(password or "", safe="")} - {None, ""}:
        text = text.replace(secret, "***")
    return text


def _is_ipv6(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).version == 6
    except ValueError:
        return False
