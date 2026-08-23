"""training_gym/teachers/cloud.py — V69 M62: the ten conditions a paid call needs.

WHY THIS IS ONE MODULE AND NOT TWO
----------------------------------
The OpenAI and Anthropic adapters differ in an endpoint, a header name and a response
shape. Everything that actually matters — the authorization gate, the credential seam,
the cost estimate, the size bounds, the audit event, the refusal to substitute a
provider — is identical, and duplicating it would mean maintaining two copies of the
security logic and eventually fixing a bug in one of them.

THE GATE
--------
:func:`authorize_cloud_call` returns a decision, not a boolean, and a call is authorized
only when EVERY condition below holds. A missing condition is a refusal, never a
downgrade to a local provider and never a substitution of a cheaper model:

  1. the provider was explicitly selected;
  2. the model was explicitly named;
  3. the operator passed the cloud flag (``--allow-cloud-teachers``);
  4. the packet's material is exportable at all;
  5. sanitization passed;
  6. the secret scan passed;
  7. the hidden-evaluation-target check passed;
  8. a cost estimate exists;
  9. a credential is reachable;
 10. the destination host is on the allowlist, over HTTPS.

DEFAULT OFF
-----------
:class:`CloudTeacherConfig` defaults ``allow_cloud_teachers`` to ``False``. There is no
value of the environment, no config file and no packet content that flips it; only an
explicit operator action does, and the refusal reason says so.

CREDENTIALS
-----------
A credential VALUE never enters this module's arguments, fields, logs, audit events or
error messages. Providers receive a *lookup* — a callable that reports whether a secret
is reachable and, at the moment of the call, hands it straight to the transport. The
lookup is injectable so a test can assert "no credential" without depending on whatever
happens to be in the operator's environment, and so a test can never accidentally use a
real one.

TRANSPORT
---------
:class:`CloudTransport` is an abstract seam. This package ships no default
implementation: shipping one would mean a bug in the gate is one line away from a real
paid request. The operator (or a test) supplies a transport, and a provider with no
transport reports itself unavailable — honestly, and without pretending it could have
called if only someone had asked.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlparse

from ..schemas import SchemaError, body_free_repr, require_bool, require_int
from .base import (
    MAX_TEACHER_PROMPT_BYTES,
    MAX_TEACHER_RESPONSE_BYTES,
    CostEstimate,
    TeacherError,
    TeacherNotAuthorized,
)

#: The only hosts a teacher request may be sent to. An allowlist, because the set of
#: correct destinations is two and the set of wrong ones is the internet.
ALLOWED_CLOUD_HOSTS: frozenset[str] = frozenset({
    "api.openai.com", "api.anthropic.com",
})

#: Bounds on a live exchange. A retry ceiling of 2 means at most three attempts, and
#: retries happen only on a transport error — never on a refusal, and never against a
#: different model.
MAX_CLOUD_TIMEOUT_S = 120
MAX_CLOUD_RETRIES = 2
#: Smallest gap between two calls to one provider, enforced by the caller's clock seam.
MIN_CALL_INTERVAL_S = 2

#: Rough token pricing, per million tokens, USD. Bumped by hand with the table version.
#: These produce an ESTIMATE whose only job is to let an operator refuse a call that
#: would cost more than they expected; they are not billing.
PRICE_TABLE_VERSION = "m62.prices.2026-08"
PRICE_TABLE: dict[str, tuple[float, float]] = {
    # model prefix: (input $/Mtok, output $/Mtok)
    "gpt-5": (1.25, 10.00),
    "gpt-4.1": (2.00, 8.00),
    "o4": (1.10, 4.40),
    "claude-opus": (15.00, 75.00),
    "claude-sonnet": (3.00, 15.00),
    "claude-haiku": (0.80, 4.00),
}
#: Used when a model is not in the table. Deliberately the MOST expensive row: an
#: estimate that guesses low is an estimate that authorises a surprise.
FALLBACK_PRICE = (15.00, 75.00)
#: Characters per token, for the estimate only. Conservative on purpose.
CHARS_PER_TOKEN = 3.5
#: What a reviewer is expected to write back. Bounded by the response ceiling anyway.
ASSUMED_COMPLETION_TOKENS = 700


class CloudTransportError(TeacherError):
    """The transport failed. Retryable; never turned into a review."""


@dataclass(frozen=True)
class CloudRequest:
    """One outbound request, fully described before anything is sent.

    Holds no credential: the transport is handed the secret separately, at the moment
    of the call, so a request object can be logged, hashed and asserted on in a test
    without ever carrying one.
    """

    url: str
    model: str
    payload: Mapping[str, object]
    timeout_s: int
    provider_id: str

    def __post_init__(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme != "https":
            raise TeacherNotAuthorized(
                f"cloud request: {parsed.scheme or 'no'} scheme is refused; a teacher "
                f"packet is only ever sent over HTTPS")
        if parsed.hostname not in ALLOWED_CLOUD_HOSTS:
            raise TeacherNotAuthorized(
                f"cloud request: host {parsed.hostname!r} is not on the allowlist "
                f"{sorted(ALLOWED_CLOUD_HOSTS)}; a redirected endpoint is an "
                f"exfiltration channel")
        require_int(self.timeout_s, "cloud request: timeout_s",
                    minimum=1, maximum=MAX_CLOUD_TIMEOUT_S)
        if not str(self.model or "").strip():
            raise TeacherNotAuthorized("cloud request: an explicit model is required")

    def size_bytes(self) -> int:
        from ..schemas import canonical_json
        return len(canonical_json(dict(self.payload)).encode("utf-8", "surrogatepass"))


@dataclass(frozen=True)
class CloudResponse:
    """What came back. Bounded, and never persisted verbatim by anything upstream."""

    status: int
    text: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:
        """Transport status only — never the response ``text``."""
        return body_free_repr(self, "status", text_len=len(self.text or ""))

    def __post_init__(self) -> None:
        require_int(self.status, "cloud response: status", minimum=100, maximum=599)
        size = len(str(self.text or "").encode("utf-8", "surrogatepass"))
        if size > MAX_TEACHER_RESPONSE_BYTES:
            raise CloudTransportError(
                f"cloud response: {size} bytes exceeds the "
                f"{MAX_TEACHER_RESPONSE_BYTES}-byte ceiling; a reviewer that returned "
                f"that much is not returning one JSON object")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class CloudTransport(ABC):
    """The seam a live call goes through. No implementation ships with this package.

    Shipping a default would put a real paid request one bug away from every code path
    in the gate above. The operator wires one deliberately; a test wires a recorder.
    """

    @abstractmethod
    def send(self, request: CloudRequest, *, api_key: str) -> CloudResponse:
        """Perform the request. Must not log, store or echo *api_key*."""


#: How a provider learns whether a credential exists. Returns the secret or ``None``.
CredentialLookup = Callable[[str], "str | None"]


def environment_credential(name: str) -> str | None:
    """Read a credential from the process environment, or report its absence.

    The default lookup, and deliberately the only one this module implements: an OS
    keychain integration belongs to the application, not to the gym, and a gym that
    knew how to open a keychain would be a gym that could open it during a test.

    A credential is NEVER accepted from a CLI argument — argv is visible to every
    process on the host and lands in shell history — so there is no code path here that
    reads one from anywhere but this function's caller-supplied variable name.
    """
    value = os.environ.get(str(name or ""), "")
    stripped = value.strip()
    return stripped or None


@dataclass(frozen=True)
class CloudTeacherConfig:
    """Everything an operator must decide before a paid call can happen.

    Every field defaults to the refusing value. A config constructed with no arguments
    authorises nothing, which is the state the gym is in until somebody types a flag.
    """

    #: The explicit operator flag, ``--allow-cloud-teachers``. Never read from an
    #: environment variable: an exported variable is a decision nobody remembers making.
    allow_cloud_teachers: bool = False
    #: Explicit per-call operator confirmation, separate from the flag above so that
    #: enabling cloud teachers for a session is not the same act as authorising a call.
    operator_confirmed: bool = False
    #: The largest estimated spend the operator has pre-approved for one call.
    max_cost_usd: float = 0.50
    timeout_s: int = 60
    max_retries: int = 1
    min_interval_s: int = MIN_CALL_INTERVAL_S

    def __post_init__(self) -> None:
        require_bool(self.allow_cloud_teachers, "cloud config: allow_cloud_teachers")
        require_bool(self.operator_confirmed, "cloud config: operator_confirmed")
        require_int(self.timeout_s, "cloud config: timeout_s",
                    minimum=1, maximum=MAX_CLOUD_TIMEOUT_S)
        require_int(self.max_retries, "cloud config: max_retries",
                    minimum=0, maximum=MAX_CLOUD_RETRIES)
        require_int(self.min_interval_s, "cloud config: min_interval_s",
                    minimum=0, maximum=3_600)
        if self.max_cost_usd < 0:
            raise SchemaError("cloud config: max_cost_usd may not be negative")

    def to_dict(self) -> dict:
        return {"allow_cloud_teachers": self.allow_cloud_teachers,
                "operator_confirmed": self.operator_confirmed,
                "max_cost_usd": round(float(self.max_cost_usd), 4),
                "timeout_s": self.timeout_s, "max_retries": self.max_retries,
                "min_interval_s": self.min_interval_s}


@dataclass(frozen=True)
class AuthorizationDecision:
    """Why a call may or may not happen. Never a bare boolean.

    A bare boolean loses the reason, and the reason is what an operator needs: "cloud
    teachers are disabled" and "your API key is missing" call for completely different
    next actions, and a caller that only saw ``False`` would report neither.
    """

    authorized: bool
    reasons: tuple[str, ...] = ()
    satisfied: tuple[str, ...] = ()
    cost: CostEstimate | None = None

    def __post_init__(self) -> None:
        if self.authorized and self.reasons:
            raise TeacherError("authorization: cannot be authorized with outstanding "
                               "reasons; a partially-satisfied gate is a closed gate")
        if not self.authorized and not self.reasons:
            raise TeacherError("authorization: a refusal must state why")

    def raise_if_refused(self) -> None:
        if not self.authorized:
            raise TeacherNotAuthorized("; ".join(self.reasons))

    def to_dict(self) -> dict:
        return {"authorized": self.authorized, "reasons": list(self.reasons),
                "satisfied": list(self.satisfied),
                "cost": self.cost.to_dict() if self.cost else None}


def estimate_cost(model: str, prompt_chars: int) -> CostEstimate:
    """An approximate price for one review, before it is requested.

    Deliberately pessimistic: an unknown model is priced at the most expensive row in
    the table, and the completion length is assumed rather than hoped for. An estimate
    that guesses low is an estimate that authorises a surprise.
    """
    name = str(model or "").strip().lower()
    rate_in, rate_out = FALLBACK_PRICE
    for prefix, rates in sorted(PRICE_TABLE.items(), key=lambda kv: -len(kv[0])):
        if name.startswith(prefix):
            rate_in, rate_out = rates
            break
    prompt_tokens = max(1, int(max(0, prompt_chars) / CHARS_PER_TOKEN))
    usd = (prompt_tokens * rate_in + ASSUMED_COMPLETION_TOKENS * rate_out) / 1_000_000
    return CostEstimate(prompt_tokens=prompt_tokens,
                        completion_tokens=ASSUMED_COMPLETION_TOKENS,
                        usd=round(usd, 6), price_table_version=PRICE_TABLE_VERSION,
                        approximate=True)


def authorize_cloud_call(*, packet: object, provider_id: str, model: str,
                         config: CloudTeacherConfig, exportable: bool,
                         credential_present: bool, url: str,
                         transport_present: bool) -> AuthorizationDecision:
    """Evaluate every condition a paid call requires. Fail closed on any of them.

    Collects ALL the reasons rather than returning at the first one: an operator who
    fixes the missing API key only to be refused for the missing flag, and then for the
    missing confirmation, learns to keep typing rather than to read.
    """
    reasons: list[str] = []
    satisfied: list[str] = []

    def check(ok: bool, name: str, reason: str) -> None:
        (satisfied if ok else reasons).append(name if ok else reason)

    check(bool(str(provider_id or "").strip()), "provider_selected",
          "no provider was explicitly selected")
    check(bool(str(model or "").strip()), "model_selected",
          "no model was explicitly named; a cloud call never resolves an alias for you")
    check(bool(config.allow_cloud_teachers), "cloud_flag",
          "cloud teachers are disabled; pass --allow-cloud-teachers to enable them for "
          "this run (they are off by default and no environment variable turns them on)")
    check(bool(config.operator_confirmed), "operator_confirmation",
          "this call was not confirmed by the operator")
    check(bool(exportable), "packet_exportable",
          "this task's material is not exportable to a cloud teacher")
    check(bool(credential_present), "credential_available",
          "no API credential is reachable for this provider (environment or OS "
          "credential storage only; never a CLI argument)")
    check(bool(transport_present), "transport_configured",
          "no HTTPS transport is wired for this provider, so no call can be made")

    packet_ok = packet is not None and hasattr(packet, "to_prompt_text")
    check(packet_ok, "packet_present", "no packet was supplied")

    prompt_chars = 0
    sanitized_ok = False
    if packet_ok:
        try:
            prompt = packet.to_prompt_text()  # type: ignore[attr-defined]
            prompt_chars = len(prompt)
            from .sanitization import scan_export_payload
            leaks = scan_export_payload(prompt)
            sanitized_ok = not leaks
            if leaks:
                reasons.append(f"the packet did not pass its export scan "
                               f"({', '.join(leaks)})")
            else:
                satisfied.append("sanitization_and_secret_scan")
            packet.verify_integrity()  # type: ignore[attr-defined]
            satisfied.append("packet_integrity")
        except TeacherError as exc:
            reasons.append(f"packet integrity: {exc}")
        except Exception as exc:  # noqa: BLE001 — an unverifiable packet is refused
            reasons.append(f"packet could not be verified: {type(exc).__name__}")

    size = int(prompt_chars)
    if size > MAX_TEACHER_PROMPT_BYTES:
        reasons.append(f"packet is {size} chars, over the "
                       f"{MAX_TEACHER_PROMPT_BYTES}-byte request ceiling")
    elif sanitized_ok:
        satisfied.append("request_size")

    cost: CostEstimate | None = None
    if str(model or "").strip():
        cost = estimate_cost(model, prompt_chars)
        if cost.usd > config.max_cost_usd:
            reasons.append(f"estimated ${cost.usd:.4f} exceeds the pre-approved "
                           f"${config.max_cost_usd:.4f} for one call")
        else:
            satisfied.append("cost_estimate")
    else:
        reasons.append("no cost estimate is available without an explicit model")

    try:
        parsed = urlparse(str(url or ""))
        destination_ok = (parsed.scheme == "https"
                          and parsed.hostname in ALLOWED_CLOUD_HOSTS)
    except ValueError:  # pragma: no cover — urlparse is total for str input
        destination_ok = False
    check(destination_ok, "destination_allowlisted",
          f"destination {url!r} is not an allowlisted HTTPS endpoint "
          f"({sorted(ALLOWED_CLOUD_HOSTS)})")

    if reasons:
        return AuthorizationDecision(authorized=False, reasons=tuple(reasons),
                                     satisfied=tuple(satisfied), cost=cost)
    return AuthorizationDecision(authorized=True, satisfied=tuple(satisfied),
                                 cost=cost)


def audit_event(*, provider_id: str, model: str, decision: AuthorizationDecision,
                packet_id: str, response_sha256: str = "",
                status: int | None = None) -> dict:
    """The record of a live-call attempt. Says that it happened, never what it said.

    No URL query, no headers, no request body, no response body — only the decision,
    the model, the packet it concerned, the HTTP status and a digest. An audit trail is
    the last place anyone looks for a leak, which makes it the first place one survives.
    """
    return {
        "event": "teacher_cloud_call",
        "provider": provider_id,
        "model": model,
        "packet_id": packet_id,
        "authorized": decision.authorized,
        "refusal_reasons": list(decision.reasons),
        "gates_satisfied": list(decision.satisfied),
        "estimated_usd": decision.cost.usd if decision.cost else None,
        "price_table_version": (decision.cost.price_table_version
                                if decision.cost else ""),
        "http_status": status,
        "response_sha256": response_sha256,
    }


__all__ = [
    "ALLOWED_CLOUD_HOSTS", "ASSUMED_COMPLETION_TOKENS", "CHARS_PER_TOKEN",
    "FALLBACK_PRICE", "MAX_CLOUD_RETRIES", "MAX_CLOUD_TIMEOUT_S",
    "MIN_CALL_INTERVAL_S", "PRICE_TABLE", "PRICE_TABLE_VERSION",
    "AuthorizationDecision", "CloudRequest", "CloudResponse", "CloudTeacherConfig",
    "CloudTransport", "CloudTransportError", "CredentialLookup",
    "audit_event", "authorize_cloud_call", "environment_credential", "estimate_cost",
]
