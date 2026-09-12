"""Fail-closed URL policy for requests carrying account credentials."""

from __future__ import annotations

import ipaddress
from urllib.parse import urljoin
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler
from urllib.request import Request


class CredentialOriginError(RuntimeError):
    """Raised before a credential can be sent outside its configured origin."""


def _canonical_host(host: str) -> str:
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        try:
            return host.encode("idna").decode("ascii").casefold()
        except UnicodeError as exc:
            raise CredentialOriginError("Credential URL has an invalid hostname.") from exc


def _credential_origin(value: str) -> tuple[str, str, int]:
    if not value or "\\" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise CredentialOriginError("Credential URL contains unsafe characters.")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
        username = parsed.username
        password = parsed.password
    except ValueError as exc:
        raise CredentialOriginError("Credential URL is malformed.") from exc
    if parsed.scheme.casefold() != "https" or not parsed.netloc or not hostname:
        raise CredentialOriginError("Credential URL must use absolute HTTPS.")
    if username is not None or password is not None or "%" in parsed.netloc or "%" in hostname:
        raise CredentialOriginError("Credential URL has an ambiguous authority.")
    return "https", _canonical_host(hostname), port if port is not None else 443


def require_credentialed_url(base_url: str, target_url: str) -> str:
    """Resolve *target_url* and require the configured HTTPS authority."""

    try:
        raw_target = urlsplit(target_url)
    except ValueError as exc:
        raise CredentialOriginError("Credential URL is malformed.") from exc
    if raw_target.scheme and not raw_target.netloc:
        raise CredentialOriginError("Credential URL has a malformed absolute form.")
    resolved = urljoin(base_url, target_url)
    if _credential_origin(base_url) != _credential_origin(resolved):
        raise CredentialOriginError("Credential URL changes the configured HTTPS origin.")
    return resolved


class SameOriginHTTPSRedirectHandler(HTTPRedirectHandler):
    """Follow credentialed redirects only while every hop remains same-origin."""

    def __init__(self, base_url: str) -> None:
        super().__init__()
        _credential_origin(base_url)
        self.base_url = base_url

    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> Request | None:
        has_authorization = any(
            name.casefold() == "authorization" for name, _ in req.header_items()
        )
        if has_authorization:
            newurl = require_credentialed_url(self.base_url, newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)
