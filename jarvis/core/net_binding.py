"""core/net_binding.py — V69 M61.7: one definition of "all interfaces".

Bandit reported twelve B104 findings ("possible binding to all interfaces") across ten
modules. They were two completely different things wearing the same string, and
treating them alike would have been wrong in both directions.

**Seven were false positives.** They compare an address; they do not bind a socket:

  ``core/arp_deception.py``      filters ARP packets whose source is the unspecified
                                address — a real, necessary check.
  ``core/asset_discovery.py``    CLASSIFIES a discovered listener as externally
                                reachable *because* it is bound to all interfaces.
                                Bandit flags the detection of the very condition it
                                cares about.
  ``core/decoy_service.py``      "which addresses are my own" set.
  ``core/tarpit_deception.py``   same.
  ``core/industrial_asset_guard.py`` skips unspecified/broadcast source addresses.
  ``core/itdr_sentinel.py``      skips unspecified/loopback addresses when parsing
                                authentication telemetry.
  ``core/network_quarantine.py`` refuses to quarantine JARVIS's own host — a SAFETY
                                interlock, so changing its semantics to satisfy a
                                scanner would be actively dangerous.

Those seven now reference the constants below. The network semantics are byte-for-byte
what they were; only the spelling moved.

**Five were real binds**, and four of them had no gate at all:
``core/decoy_service.py``, ``core/tarpit_deception.py``, ``tools/active_tarpit.py`` and
``core/dns_sinkhole.py`` bound ``0.0.0.0`` unconditionally, so starting JARVIS on a
laptop published decoy SSH/SMB/RDP/MSSQL listeners and a DNS server to whatever network
that laptop was on — a coffee-shop Wi-Fi included. ``core/canary.py`` already had the
right shape (V68.1 M50: loopback by default, exposure behind an explicit operator
opt-in, warned and logged). :func:`resolve_bind_host` generalises that proven pattern so
all five services share it instead of four of them having nothing.

THE POLICY
----------
  * default is **loopback** — a deception service that is not deliberately exposed
    listens only to its own host;
  * all-interface exposure requires an explicit ``JARVIS_<SERVICE>_EXPOSE`` opt-in;
  * an operator may instead name a single specific address to bind, which is narrower
    than all-interfaces and is honoured as-is;
  * every exposure logs a WARNING naming the service and the proven bind address, so
    an exposed listener is never silent.
"""
from __future__ import annotations

import os

from loguru import logger

# ── the addresses, defined exactly once ─────────────────────────────────────
#
# B104 SUPPRESSION JUSTIFICATION (the directive is on the assignment line below and
# names exactly one test id):
#   This is a NAMED CONSTANT DECLARATION. It binds nothing — there is no socket, no
#   bind(), no listen() and no server anywhere in this module. Its whole purpose is to
#   give the ten modules that previously repeated the literal a single place to import
#   it from, so "what does all-interfaces mean" and "when may we bind it" are defined
#   together and reviewed together.
#   There is no safer equivalent expression: Bandit B104 matches the string literal
#   itself, so any spelling of the correct value is flagged, and computing it
#   (``str(ipaddress.ip_address(0))``) would only hide the value from the reader as
#   well as from the scanner — strictly worse for review.
#   tests/test_network_binding_v69_m617.py proves the property this claims: no bind /
#   listen / connect / socket call exists in this module, and the tree contains exactly
#   one B104 suppression.
ALL_INTERFACES_V4 = "0.0.0.0"  # nosec B104
ALL_INTERFACES_V6 = "::"
LOOPBACK_V4 = "127.0.0.1"
LOOPBACK_V6 = "::1"
BROADCAST_V4 = "255.255.255.255"

#: Every spelling of "not a specific interface" this codebase encounters. ``"*"`` shows
#: up in ``netstat``-style output that ``asset_discovery`` parses.
UNSPECIFIED_HOSTS: frozenset[str] = frozenset({
    ALL_INTERFACES_V4, ALL_INTERFACES_V6, "*",
})

#: Addresses that mean "this machine". Used by the quarantine safety interlock and by
#: the "is that listener mine" checks.
OWN_HOST_ADDRESSES: frozenset[str] = frozenset({
    LOOPBACK_V4, LOOPBACK_V6, "localhost", ALL_INTERFACES_V4,
})

_TRUE = frozenset({"1", "true", "yes", "on"})


def is_all_interfaces(host: str | None) -> bool:
    """True if ``host`` means "every interface" rather than a specific address."""
    return (host or "").strip() in UNSPECIFIED_HOSTS


def exposure_requested(expose_env: str) -> bool:
    """Whether the operator explicitly opted this service into non-local exposure."""
    return os.environ.get(expose_env, "").strip().lower() in _TRUE


def resolve_bind_host(
    service: str,
    *,
    expose_env: str,
    bind_env: str | None = None,
    default_host: str = LOOPBACK_V4,
) -> str:
    """Resolve the address ``service`` should bind. Loopback unless told otherwise.

    ``expose_env``   env var that opts the service into non-local exposure.
    ``bind_env``     optional env var naming a SPECIFIC address to bind when exposed;
                     narrower than all-interfaces, so it is honoured verbatim.
    ``default_host`` the safe default. Loopback; overridable only in code, never by
                     the environment, so a stray env var cannot widen the default.

    Any exposure is logged at WARNING with the proven bind address — an exposed decoy
    listener that nobody knows about is the failure mode this exists to prevent.
    """
    if not exposure_requested(expose_env):
        return default_host

    explicit = os.environ.get(bind_env, "").strip() if bind_env else ""
    host = explicit or ALL_INTERFACES_V4

    if is_all_interfaces(host):
        logger.warning(
            f"{service}: authorized exposure ENABLED via {expose_env} — binding ALL "
            f"interfaces ({host}). This service is reachable from every network this "
            f"host is attached to. Confirm this is an authorized lab network; set "
            f"{bind_env or '<service>_BIND'} to a single address to narrow it."
        )
    else:
        logger.warning(
            f"{service}: authorized exposure ENABLED via {expose_env} — binding "
            f"{host} (reachable off-host). Ensure this is an authorized lab network."
        )
    return host
