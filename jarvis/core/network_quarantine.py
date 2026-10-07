"""
core/network_quarantine.py — JARVIS V48.0 VANGUARD
XDR containment. On correlator-flagged lateral movement / scanning (sev >= 9.0)
from a local IP, isolates that host with bidirectional Windows Firewall rules
(endpoint-local) and, where infrastructure is configured, escalates to a
switchport/NAC quarantine webhook. Reversible; every action is audited.

Containment is enforced at the endpoint and (optionally) the network
infrastructure API — it does NOT manipulate the L2 state of other hosts.

V69 M68D (H04): EFFECT TRUTH
============================
A quarantine is TWO firewall rules, and a release is two deletions. Collapsing
either into one optimistic boolean made the receipt lie in both directions.
MEASURED with a deterministic fake backend (no netsh was invoked):

* ``release()`` discarded both ``_run`` return codes, then did
  ``_active.pop(ip)`` and ``res["released"] = True`` unconditionally. All three
  scenarios — both deletions failed, one failed, both succeeded — returned the
  byte-identical ``{"released": True}``. The return value carried no information
  about what happened, and local tracking was erased either way, so a rule that
  is still blocking traffic became invisible AND unreleasable.
* ``release()`` wrote no audit receipt at all. Four receipts existed for four
  ADD scenarios and zero for three RELEASE scenarios.
* ``quarantine()`` with the IN rule succeeding and the OUT rule failing set
  ``host_isolated = False`` and did NOT record the target in ``_active``. One
  real firewall rule existed and nothing in the process knew about it.

So the module now distinguishes NO / FULL / PARTIAL / UNKNOWN effect and
RECONCILIATION_REQUIRED, keeps PER-RULE evidence, and VERIFIES by querying the
firewall rather than trusting a return code. Each rule's evidence carries M65D's
:class:`~core.effect_journal.ExternalOutcome`, which is reused rather than
reinvented: a non-zero ``netsh`` exit does NOT prove nothing happened (the
wrapper returns ``(1, …)`` for a timeout too), so it is ``UNKNOWN`` until a query
says otherwise.

WHAT THIS DOES NOT CLAIM (§27)
------------------------------
``_active`` is PROCESS-LOCAL. It is not a durable journal and this module does
not add one: a process restart still loses the in-memory view of managed rules,
and the honest recovery is a query of the firewall, which
:func:`pending_reconciliation` exposes rather than pretending to have persisted.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path

from core.rbac_manager import ClearanceLevel, requires_clearance

logger = logging.getLogger("jarvis.network_quarantine")

_IS_WINDOWS = os.name == "nt"

try:
    import psutil
    _PSUTIL_OK = True
except Exception:
    psutil = None
    _PSUTIL_OK = False

# --- Config ------------------------------------------------------------------
_QUARANTINE_ENABLED = True
_AUTO_THRESHOLD = 9.0
_MAX_ACTIVE = 16
_RULE_PREFIX = "JARVIS-QUARANTINE"
from core.managed_paths import log_artifact_path
from core import net_binding


def _log_path() -> Path:
    """Managed, installation-owned audit trail (V69 M61 RC1) — see punisher.

    Host isolation is an effectful, operator-visible action; its record must live
    at one installation-owned location regardless of where JARVIS was launched.
    """
    return log_artifact_path("network_quarantine.jsonl")


_NAC_WEBHOOK = os.environ.get("JARVIS_NAC_WEBHOOK")        # optional NAC/switch API
_LAB_SUBNET = os.environ.get("JARVIS_LAB_SUBNET", "192.168.1.0/24")

#: Process-local view of the rules this process believes it manages. Maps ip ->
#: the containment record that created them. It holds PARTIAL effects too: a
#: half-created quarantine is exactly the thing that must not go untracked.
_active: dict = {}
_lock = asyncio.Lock()


class ContainmentEffect(str, Enum):
    """How much of a two-rule containment operation actually landed.

    This is NOT a competing vocabulary for M65D's
    :class:`~core.effect_journal.ExternalOutcome`: that type answers "is THIS
    effect in the external world", per rule, and is carried per rule in
    :class:`RuleEvidence`. This answers the different question M65D does not —
    "how many of the rules this operation is made of are there now" — and is
    DERIVED from those per-rule outcomes.
    """

    #: Nothing is in place. Proven, not assumed.
    NO_EFFECT = "NO_EFFECT"
    #: Every rule the operation is made of is verified in its target state.
    FULL_EFFECT = "FULL_EFFECT"
    #: Some rules landed and some did not. A real, asymmetric firewall state.
    PARTIAL_EFFECT = "PARTIAL_EFFECT"
    #: At least one rule's state could not be established either way.
    UNKNOWN_EFFECT = "UNKNOWN_EFFECT"
    #: Known-inconsistent and needs an operator or a retry. Never silently
    #: rounded to success or to NO_EFFECT.
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


#: M65D outcome strings, imported lazily so this module stays importable without
#: the journal's sqlite machinery (same reasoning as `core.source_integrity`).
_PROVEN_COMMITTED = "PROVEN_COMMITTED"
_PROVEN_NOT_EXECUTED = "PROVEN_NOT_EXECUTED"
_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class RuleEvidence:
    """What is known about ONE firewall rule after ONE attempt.

    ``command_rc`` is what the command said. ``observed_present`` is what a
    QUERY said, and ``None`` means no query was possible — the difference
    between "I looked" and "I assumed" is the whole point of the dataclass.
    """

    name: str
    direction: str
    action: str
    attempted: bool
    command_rc: "int | None"
    #: An M65D :class:`~core.effect_journal.ExternalOutcome` value, as a string.
    outcome: str
    #: ``True``/``False`` from a firewall query; ``None`` when unverified.
    observed_present: "bool | None" = None

    def to_dict(self) -> dict:
        return asdict(self)


def _outcome_for(rc: "int | None", present: "bool | None", *,
                 want_present: bool) -> str:
    """The M65D outcome for one rule.

    A QUERY outranks a return code: it is evidence about the world rather than
    about our invocation. Without a query, ``rc == 0`` is the command's own
    receipt and anything else is ``UNKNOWN`` — a non-zero exit from `_run` also
    covers a timeout, which proves nothing about whether the rule was created.
    """
    if present is not None:
        if present is want_present:
            return _PROVEN_COMMITTED
        return _PROVEN_NOT_EXECUTED
    if rc == 0:
        return _PROVEN_COMMITTED
    return _UNKNOWN


def _derive_effect(rules: "list[RuleEvidence]") -> ContainmentEffect:
    """Fold per-rule outcomes into the operation's coverage.

    UNKNOWN is contagious and is never rounded: a mixed set that contains an
    unknown is RECONCILIATION_REQUIRED, because "some rules are there and I
    cannot tell about the rest" is the state an operator has to act on.
    """
    if not rules:
        return ContainmentEffect.NO_EFFECT
    outcomes = [r.outcome for r in rules]
    committed = sum(1 for o in outcomes if o == _PROVEN_COMMITTED)
    absent = sum(1 for o in outcomes if o == _PROVEN_NOT_EXECUTED)
    unknown = sum(1 for o in outcomes if o == _UNKNOWN)
    if unknown and committed:
        return ContainmentEffect.RECONCILIATION_REQUIRED
    if unknown:
        return ContainmentEffect.UNKNOWN_EFFECT
    if committed == len(rules):
        return ContainmentEffect.FULL_EFFECT
    if absent == len(rules):
        return ContainmentEffect.NO_EFFECT
    return ContainmentEffect.PARTIAL_EFFECT


def _rule_name(ip: str, direction: str) -> str:
    return f"{_RULE_PREFIX}-{ip}-{direction.upper()}"


def _rule_present(name: str) -> "bool | None":
    """Query the firewall for *name*. ``None`` means the query did not answer.

    ``netsh advfirewall firewall show rule`` exits non-zero when no rule
    matches, which is the ABSENCE answer rather than a failure. The two are only
    distinguishable because the wrapper returns its own ``(1, <exception>)`` for
    a timeout or a missing binary, and that case is reported as unknown.
    """
    rc, out = _run(["netsh", "advfirewall", "firewall", "show", "rule",
                    f"name={name}"])
    if rc == 0:
        return True
    low = (out or "").lower()
    if "no rules match" in low or "ninguna regla" in low or "no rule" in low:
        return False
    # An exit code we cannot interpret proves nothing either way.
    return None


def _is_admin() -> bool:
    if not _IS_WINDOWS:
        return hasattr(os, "geteuid") and os.geteuid() == 0
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _local_ips() -> set:
    ips = set()
    if _PSUTIL_OK:
        try:
            for addrs in psutil.net_if_addrs().values():
                for a in addrs:
                    if getattr(a, "address", None):
                        ips.add(a.address.split("%")[0])
        except Exception:
            pass
    return ips


def _gateways() -> set:
    gws = set()
    try:
        net = ipaddress.ip_network(_LAB_SUBNET, strict=False)
        gws.add(str(next(net.hosts())))   # .1 convention — never block the gateway
    except Exception:
        pass
    return gws


def _is_protected(ip: str) -> bool:
    # SAFETY INTERLOCK: never quarantine JARVIS's own host. Bandit flagged the
    # "0.0.0.0" literal here (B104); nothing binds. The set is unchanged — altering
    # it to satisfy a scanner could let JARVIS isolate itself off the network.
    if ip in net_binding.OWN_HOST_ADDRESSES:
        return True
    if ip in _local_ips() or ip in _gateways():
        return True
    try:
        a = ipaddress.ip_address(ip)
        if a.is_loopback or a.is_multicast or a.is_unspecified:
            return True
    except ValueError:
        return True  # not a valid IP — refuse
    return False


def _valid_ip(ip: str) -> bool:
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


def _run(cmd: list[str]):
    """argv-vector execution — no shell, so ip/rule-name content can never be
    reinterpreted as command syntax. Callers must validate ip with
    _valid_ip() before building cmd (defense in depth on top of that)."""
    try:
        p = subprocess.run(cmd, shell=False, capture_output=True, text=True, timeout=15)
        return p.returncode, (p.stdout + p.stderr).strip()
    except Exception as e:
        return 1, str(e)


def _audit(res: dict) -> None:
    try:
        with _log_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(res, default=str) + "\n")
    except Exception as e:
        logger.debug("network_quarantine: audit write failed: %s", e)


async def _nac_isolate(ip: str, reason: str) -> bool:
    if not _NAC_WEBHOOK:
        return False
    loop = asyncio.get_running_loop()

    def _post():
        # V69 M61.7 (Bandit B310): JARVIS_NAC_WEBHOOK is operator configuration that
        # reaches `urlopen`, whose default opener also serves file:/ftp:/data:. A
        # `file://` value would make this "isolation" silently succeed against a local
        # file while the host stayed on the network — a containment action reported as
        # done and never performed.
        #
        # Unlike the Ollama probes this is NOT pinned to a local destination: a NAC
        # appliance or SaaS controller is legitimately remote. Everything else applies
        # — http/https only, no embedded credentials, no fragment, valid port, and
        # every redirect hop re-validated. The destination class is logged so an
        # operator can see whether containment left their network.
        import urllib.request

        from core.url_policy import UrlPolicyError, describe, open_url
        body = json.dumps({"action": "quarantine", "ip": ip, "reason": reason}).encode()
        try:
            req = urllib.request.Request(_NAC_WEBHOOK, data=body,
                                         headers={"Content-Type": "application/json"})
            with open_url(req, timeout=10, label="nac_webhook") as r:
                logger.info("network_quarantine: NAC webhook target=%s",
                            describe(_NAC_WEBHOOK))
                return 200 <= getattr(r, "status", 200) < 300
        except UrlPolicyError as e:
            logger.error("network_quarantine: NAC webhook URL refused by policy: %s", e)
            return False
        except Exception as e:
            logger.error("network_quarantine: NAC webhook failed: %s", e)
            return False

    return await loop.run_in_executor(None, _post)


async def _report(correlator, ip: str, reason: str, res: dict) -> None:
    event = {"source": "network_quarantine", "type": "host_quarantined",
             "severity": 8.0, "ip": ip, "reason": reason,
             "host_isolated": res.get("host_isolated"),
             "nac_isolated": res.get("nac_isolated"), "ts": time.time()}
    try:
        if hasattr(correlator, "ingest_event"):
            await correlator.ingest_event(event)
        elif hasattr(correlator, "add_event"):
            r = correlator.add_event(event)
            if asyncio.iscoroutine(r):
                await r
    except Exception as e:
        logger.error("network_quarantine: report dispatch failed: %s", e)


def _skipped(ip: str, reason: str, *, extra: "dict | None" = None) -> dict:
    """A refusal receipt. It never crossed the boundary, so the effect is NONE.

    ``PROVEN_NOT_EXECUTED`` is correct here and nowhere else in this module: no
    command ran, so nothing can be in the external world.
    """
    res = {"ip": ip, "ts": time.time(), "skipped": reason,
           "host_isolated": False, "nac_isolated": False,
           "effect": ContainmentEffect.NO_EFFECT.value,
           "outcome": _PROVEN_NOT_EXECUTED,
           "reconciliation_required": False, "rules": []}
    if extra:
        res.update(extra)
    _audit(res)
    return res


@requires_clearance(ClearanceLevel.L3_Hunter)
async def quarantine(ip: str, *, reason: str = "manual", correlator=None) -> dict:
    """Block *ip* bidirectionally and report WHICH rules exist afterwards.

    ``host_isolated`` is now true only for a verified FULL effect. A partial
    effect is reported as one AND tracked in ``_active``, because a single IN
    rule blocking traffic that no part of the process knows about is worse than
    no rule at all.
    """
    if not _QUARANTINE_ENABLED:
        return _skipped(ip, "disabled", extra={"reason": reason})
    if not (_IS_WINDOWS and _is_admin()):
        return _skipped(ip, "no admin / unsupported", extra={"reason": reason})
    if not _valid_ip(ip):
        return _skipped(ip, "invalid IP literal", extra={"reason": reason})
    if _is_protected(ip):
        return _skipped(ip, "protected infra/self IP", extra={"reason": reason})

    async with _lock:
        if ip in _active:
            tracked = _active.get(ip) or {}
            res = {"ip": ip, "reason": reason, "ts": time.time(),
                   "skipped": "already quarantined",
                   "host_isolated": bool(tracked.get("host_isolated")),
                   "nac_isolated": bool(tracked.get("nac_isolated")),
                   "effect": tracked.get("effect",
                                         ContainmentEffect.UNKNOWN_EFFECT.value),
                   "outcome": tracked.get("outcome", _UNKNOWN),
                   "reconciliation_required": bool(
                       tracked.get("reconciliation_required")),
                   "rules": tracked.get("rules", [])}
            # Was silently un-audited. An idempotent request is still a request.
            _audit(res)
            return res
        if len(_active) >= _MAX_ACTIVE:
            return _skipped(ip, "max active quarantines reached",
                            extra={"reason": reason})

        loop = asyncio.get_running_loop()
        rules: list[RuleEvidence] = []
        for direction, dir_flag in (("IN", "dir=in"), ("OUT", "dir=out")):
            name = _rule_name(ip, direction)
            cmd = ["netsh", "advfirewall", "firewall", "add", "rule",
                   f"name={name}", dir_flag, "action=block", f"remoteip={ip}"]
            rc, out = await loop.run_in_executor(None, _run, cmd)
            if rc != 0:
                logger.error("network_quarantine: netsh failed: %s | %s", cmd, out)
            # VERIFY. The return code is the command's claim; this is the world's.
            present = await loop.run_in_executor(None, _rule_present, name)
            rules.append(RuleEvidence(
                name=name, direction=direction.lower(), action="add",
                attempted=True, command_rc=rc,
                outcome=_outcome_for(rc, present, want_present=True),
                observed_present=present))

        effect = _derive_effect(rules)
        res = {
            "ip": ip, "reason": reason, "ts": time.time(), "skipped": None,
            "host_isolated": effect is ContainmentEffect.FULL_EFFECT,
            "nac_isolated": False,
            "effect": effect.value,
            "outcome": (_PROVEN_COMMITTED if effect is ContainmentEffect.FULL_EFFECT
                        else _PROVEN_NOT_EXECUTED
                        if effect is ContainmentEffect.NO_EFFECT else _UNKNOWN),
            "reconciliation_required": effect in (
                ContainmentEffect.PARTIAL_EFFECT,
                ContainmentEffect.UNKNOWN_EFFECT,
                ContainmentEffect.RECONCILIATION_REQUIRED),
            "rules": [r.to_dict() for r in rules],
        }
        # TRACK ANY REAL OR UNPROVEN EFFECT, not just the clean success. The
        # measured failure was IN-ok/OUT-failed vanishing from `_active`.
        if effect is not ContainmentEffect.NO_EFFECT:
            _active[ip] = res
        res["nac_isolated"] = await _nac_isolate(ip, reason)
        _audit(res)

    if res["host_isolated"]:
        logger.critical("NETWORK_QUARANTINE: isolated %s (%s)", ip, reason)
    elif res["reconciliation_required"]:
        logger.critical(
            "NETWORK_QUARANTINE: %s left in %s — reconciliation required (%s)",
            ip, res["effect"], reason)
    if (res["host_isolated"] or res["reconciliation_required"]) and correlator is not None:
        await _report(correlator, ip, reason, res)
    return res


@requires_clearance(ClearanceLevel.L3_Hunter)
async def release(ip: str) -> dict:
    """Remove *ip*'s rules and report whether they are ACTUALLY gone.

    ``released`` is true only when both controlled rules are VERIFIED absent.
    Local tracking survives anything less, so a rule that is still blocking
    traffic stays visible and retryable instead of being forgotten with a
    success receipt on top of it — which is what this function used to do
    unconditionally, ignoring both deletion return codes.
    """
    res = {"ip": ip, "released": False, "ts": time.time(),
           "effect": ContainmentEffect.NO_EFFECT.value,
           "outcome": _PROVEN_NOT_EXECUTED,
           "reconciliation_required": False, "rules": [], "still_tracked": False}
    if not _valid_ip(ip):
        res["error"] = "invalid IP literal"
        _audit(res)
        return res
    if not (_IS_WINDOWS and _is_admin()):
        res["error"] = "no admin / unsupported"
        res["outcome"] = _PROVEN_NOT_EXECUTED
        res["still_tracked"] = ip in _active
        res["reconciliation_required"] = res["still_tracked"]
        _audit(res)
        return res

    async with _lock:
        loop = asyncio.get_running_loop()
        rules: list[RuleEvidence] = []
        for direction in ("IN", "OUT"):
            name = _rule_name(ip, direction)
            cmd = ["netsh", "advfirewall", "firewall", "delete", "rule",
                   f"name={name}"]
            rc, out = await loop.run_in_executor(None, _run, cmd)
            if rc != 0:
                logger.error("network_quarantine: netsh delete failed: %s | %s",
                             cmd, out)
            present = await loop.run_in_executor(None, _rule_present, name)
            # For a delete the TARGET state is absence, so `want_present=False`.
            rules.append(RuleEvidence(
                name=name, direction=direction.lower(), action="delete",
                attempted=True, command_rc=rc,
                outcome=_outcome_for(rc, present, want_present=False),
                observed_present=present))

        # Every rule must be PROVEN ABSENT. A `rc == 0` with no query behind it
        # is the command's own word, and that is what used to be enough.
        proven_absent = all(r.observed_present is False for r in rules)
        effect = (ContainmentEffect.FULL_EFFECT if proven_absent
                  else _derive_effect(rules))
        res["rules"] = [r.to_dict() for r in rules]
        res["effect"] = effect.value
        res["released"] = proven_absent
        if proven_absent:
            res["outcome"] = _PROVEN_COMMITTED
        elif all(r.observed_present is True for r in rules):
            # Every controlled rule was SEEN still in place. That is not an
            # unknown: the release provably did not happen.
            res["outcome"] = _PROVEN_NOT_EXECUTED
        else:
            res["outcome"] = _UNKNOWN
        res["reconciliation_required"] = not proven_absent
        if proven_absent:
            _active.pop(ip, None)
        res["still_tracked"] = ip in _active
        _audit(res)

    if res["released"]:
        logger.info("network_quarantine: released %s", ip)
    else:
        logger.critical(
            "NETWORK_QUARANTINE: release of %s is NOT proven (%s); still tracked=%s",
            ip, res["effect"], res["still_tracked"])
    return res


def pending_reconciliation() -> dict:
    """The targets this PROCESS believes it manages but cannot call clean.

    Read-only, and deliberately not a durability claim (§27): ``_active`` dies
    with the process. What it gives an operator (and a retry) is the set of
    targets whose controlled rules were last seen partial or unproven, so the
    honest recovery — query the firewall — has somewhere to start.
    """
    return {
        ip: {"effect": record.get("effect"),
             "reconciliation_required": bool(record.get("reconciliation_required")),
             "rules": record.get("rules", [])}
        for ip, record in _active.items()
        if record.get("reconciliation_required")
        or record.get("effect") != ContainmentEffect.FULL_EFFECT.value
    }


def active_targets() -> dict:
    """A copy of the process-local containment view. Never the live dict."""
    return {ip: dict(record) for ip, record in _active.items()}


async def start(correlator=None) -> None:
    """main.py startup hook. JARVIS Watchdog Pattern: dormant if non-Windows or
    not elevated (netsh firewall + NAC actions require admin)."""
    if not _IS_WINDOWS:
        logger.warning("NETWORK_QUARANTINE: non-Windows host — dormant")
        await asyncio.Event().wait(); return
    if not _is_admin():
        logger.warning("NETWORK_QUARANTINE: not elevated (admin required) — dormant")
        await asyncio.Event().wait(); return
    try:
        _log_path()
    except Exception as e:
        logger.warning("NETWORK_QUARANTINE: log path unavailable (%s) — dormant", e)
        await asyncio.Event().wait(); return
    if correlator is not None and hasattr(correlator, "register_responder"):
        try:
            correlator.register_responder("network_quarantine", quarantine)
        except Exception:
            pass
    logger.info("NETWORK_QUARANTINE: armed — host-firewall containment%s",
                " + NAC webhook" if _NAC_WEBHOOK else "")
    await asyncio.Event().wait()
