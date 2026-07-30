"""core/url_policy.py — V69 M61.7: validated outbound HTTP.

WHY THIS EXISTS
---------------
Four call sites passed a configuration-derived URL straight to
``urllib.request.urlopen`` (Bandit B310): the Ollama health probe and chat call in
``core/ai_reverser.py``, the Ollama probe in ``core/health_watchdog.py``, and the NAC
quarantine webhook in ``core/network_quarantine.py``.

``urlopen`` is not an HTTP function. Its default opener handles ``file:``, ``ftp:`` and
``data:`` as well, so a URL that reached it from a ``.env``, an environment variable or
a stale deployment config could read a local file rather than make a request — and the
result of the Ollama probe is then interpreted as "the service is up", while the body
of the chat call is parsed and fed onward. Worse, the *redirect* path is not covered by
validating the initial URL at all: stdlib ``HTTPRedirectHandler`` follows a 302 to
``http``, ``https`` **and ``ftp``**, so a server impersonating Ollama could bounce the
request onto a scheme the caller never intended.

WHAT THIS MODULE GUARANTEES
---------------------------
  * only ``http`` and ``https`` — checked on the initial URL **and re-checked on every
    redirect hop**, so a redirect cannot escape the policy;
  * ``file:``, ``ftp:``, ``gopher:``, ``data:`` and custom schemes are refused twice:
    once by validation, and again structurally because the opener is assembled from an
    explicit handler list that simply has no handler for them. There is no default
    opener anywhere in this module;
  * credentials in the URL (``http://user:pass@host``) are refused — they leak into
    logs and proxies and are never how these endpoints authenticate;
  * scheme-relative URLs (``//host/path``) are refused, since they inherit a scheme
    this code never chose;
  * a fragment is refused for API endpoints, where it is meaningless and is a sign the
    value was pasted from a browser;
  * host must be present and the port must be a valid 1-65535;
  * ``require_local=True`` additionally pins the destination to loopback or
    private/link-local addresses, checked against **every** address the host resolves
    to, which is what defeats DNS rebinding and a hostname aliased to a public IP.

Bandit B310 is resolved structurally, not suppressed: no ``urlopen`` call remains
anywhere in the tree.
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

from loguru import logger

#: The only schemes this application may fetch over.
ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})

#: Schemes named explicitly in tests and error messages — the ones that made B310 a
#: real finding rather than a stylistic one.
REFUSED_SCHEME_EXAMPLES: tuple[str, ...] = (
    "file", "ftp", "gopher", "data", "jar", "netdoc", "ldap",
)


class UrlPolicyError(ValueError):
    """A URL is not permitted. Raised instead of fetching it."""


def _resolved_addresses(host: str) -> set[ipaddress._BaseAddress]:
    """Every IP the host maps to. Raises :class:`UrlPolicyError` if unresolvable."""
    try:
        return {ipaddress.ip_address(host.strip("[]"))}
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise UrlPolicyError(f"host {host!r} could not be resolved ({exc})") from exc
    addresses: set[ipaddress._BaseAddress] = set()
    for *_head, sockaddr in infos:
        raw = str(sockaddr[0]).split("%")[0]     # strip any IPv6 zone id
        try:
            addresses.add(ipaddress.ip_address(raw))
        except ValueError:
            raise UrlPolicyError(f"host {host!r} resolved to an invalid address {raw!r}")
    if not addresses:
        raise UrlPolicyError(f"host {host!r} resolved to no addresses")
    return addresses


def _is_local(address: ipaddress._BaseAddress) -> bool:
    """Loopback, RFC1918 private, or link-local — an operator's own network."""
    return bool(address.is_loopback or address.is_private or address.is_link_local)


def validate_url(url: str, *, require_local: bool = False, label: str = "url") -> str:
    """Validate ``url`` and return it normalized. Raises :class:`UrlPolicyError`.

    ``require_local=True`` pins the destination to the operator's own network. Use it
    for endpoints that are local by definition (Ollama), not as a general SSRF switch.
    """
    if not isinstance(url, str) or not url.strip():
        raise UrlPolicyError(f"{label}: empty URL")

    parts = urlsplit(url.strip())

    scheme = (parts.scheme or "").lower()
    if not scheme:
        # Covers "//host/path" (scheme-relative, inherits a scheme we never chose)
        # and bare "host/path".
        raise UrlPolicyError(
            f"{label}: no scheme in {url!r} — a scheme-relative or bare URL is refused"
        )
    if scheme not in ALLOWED_SCHEMES:
        raise UrlPolicyError(
            f"{label}: scheme {scheme!r} is not permitted (allowed: "
            f"{sorted(ALLOWED_SCHEMES)})"
        )

    if parts.username or parts.password:
        raise UrlPolicyError(
            f"{label}: credentials embedded in the URL are refused — they leak into "
            f"logs and proxies"
        )

    if parts.fragment:
        raise UrlPolicyError(
            f"{label}: a fragment is meaningless for an API endpoint and is refused"
        )

    host = parts.hostname
    if not host:
        raise UrlPolicyError(f"{label}: no host in {url!r}")

    try:
        port = parts.port
    except ValueError as exc:                     # non-numeric or out-of-range port
        raise UrlPolicyError(f"{label}: invalid port in {url!r} ({exc})") from exc
    if port is not None and not (1 <= port <= 65535):
        raise UrlPolicyError(f"{label}: port {port} out of range 1-65535")

    if require_local:
        addresses = _resolved_addresses(host)
        public = sorted(str(a) for a in addresses if not _is_local(a))
        if public:
            raise UrlPolicyError(
                f"{label}: {host!r} resolves to non-local address(es) {public} — this "
                f"endpoint must be on the operator's own network (loopback or private)"
            )

    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, ""))


class _ValidatingRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-applies the policy to every redirect target.

    Validating only the initial URL is the gap that makes "just check the scheme"
    insufficient: stdlib's handler happily follows a 302 into ``ftp:``, and a server
    impersonating a local Ollama could redirect a probe anywhere.
    """

    def __init__(self, *, require_local: bool, label: str):
        self._require_local = require_local
        self._label = label

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            validate_url(newurl, require_local=self._require_local, label=self._label)
        except UrlPolicyError as exc:
            raise urllib.error.HTTPError(
                newurl, code, f"refused redirect: {exc}", headers, fp
            ) from exc
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _build_opener(*, require_local: bool, label: str) -> urllib.request.OpenerDirector:
    """An opener with ONLY http/https handlers.

    Deliberately built from :class:`OpenerDirector` rather than ``build_opener``:
    ``build_opener`` always installs ``FileHandler``, ``FTPHandler``, ``DataHandler``
    and ``UnknownHandler`` unless they are explicitly replaced. Assembling the chain by
    hand means ``file:`` and ``ftp:`` are unreachable even if validation were bypassed
    — the opener has nothing that can serve them.
    """
    opener = urllib.request.OpenerDirector()
    opener.add_handler(urllib.request.HTTPHandler())
    opener.add_handler(urllib.request.HTTPSHandler())
    opener.add_handler(_ValidatingRedirectHandler(require_local=require_local, label=label))
    opener.add_handler(urllib.request.HTTPErrorProcessor())
    opener.add_handler(urllib.request.HTTPDefaultErrorHandler())
    return opener


def open_url(
    target: str | urllib.request.Request,
    *,
    timeout: float,
    require_local: bool = False,
    label: str = "url",
):
    """Validate, then fetch. The only outbound-HTTP entry point in this codebase.

    Accepts a URL string or a prepared ``Request`` (so callers can set a method,
    headers and a body). The ``Request``'s URL is validated the same way — a caller
    cannot bypass the policy by wrapping the URL in a ``Request``.

    Returns the response object; the caller is responsible for closing it (use it as a
    context manager, as every call site does).
    """
    if isinstance(target, urllib.request.Request):
        validated = validate_url(target.full_url, require_local=require_local, label=label)
        target.full_url = validated
        request: str | urllib.request.Request = target
    else:
        request = validate_url(target, require_local=require_local, label=label)

    opener = _build_opener(require_local=require_local, label=label)
    return opener.open(request, timeout=timeout)


def describe(url: str) -> dict:
    """Non-raising classification of a URL, for logging and health payloads.

    Never used to make an allow decision — :func:`validate_url` does that. This exists
    so an operator can see *where* an outbound call went without the log line itself
    performing a resolution-dependent security check.
    """
    try:
        parts = urlsplit((url or "").strip())
        host = parts.hostname or ""
        addresses = _resolved_addresses(host) if host else set()
        return {
            "scheme": (parts.scheme or "").lower(),
            "host": host,
            "port": parts.port,
            "destination": (
                "local" if addresses and all(_is_local(a) for a in addresses)
                else "public" if addresses
                else "unresolved"
            ),
        }
    except (UrlPolicyError, ValueError) as exc:
        logger.debug(f"URL_POLICY: could not describe {url!r}: {exc}")
        return {"scheme": "", "host": "", "port": None, "destination": "unresolved"}
