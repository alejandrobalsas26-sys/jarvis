"""core/tool_approval.py — V69 M66A.1 (L1): structured HITL approval + binding.

WHY THIS EXISTS
---------------
The operator's approval prompt showed ``str(tool_input)[:200]`` — the first 200
characters of the raw dict. Two actions that agree for 200 characters and diverge
dangerously afterwards produced an IDENTICAL operator-visible descriptor (§F5).
And nothing bound the reviewed action to the executed one: approval keyed only on
the tool name and that truncated string (§F19).

WHAT THIS PROVIDES
------------------
* ``ToolApprovalDescriptor`` — a typed contract carrying the security-relevant
  fields (target resources, a payload byte length, a payload DIGEST over the whole
  payload, a redacted summary) and, crucially, a ``call_digest`` over the EFFECTIVE
  call. Two payloads that diverge anywhere produce different digests and different
  rendered targets, so the collision that defeated the 200-char preview cannot
  occur.
* Redaction: sensitive values (authorization/cookie/token/secret/password/api-key
  shaped keys) are shown as ``<redacted:N bytes>`` — the operator sees THAT a
  secret is present and its size, never the secret. Secrets are not sent to
  telemetry (§18).
* ``bind_matches`` — the approval-to-execution binding (§19). The digest computed
  at challenge time must equal the digest of the call about to run; a mismatch is
  refused. REVIEWED ACTION == EXECUTED ACTION.

This module hashes and renders; it never decides authority on its own. The gate in
tools/executor.py owns the decision and calls this to describe and to verify the
binding.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field

#: Argument keys whose VALUE is a secret and must never be rendered or sent to
#: telemetry. Matched case-insensitively as a substring of the key name, so
#: `x_api_key`, `authorization`, `db_password` all redact.
_SENSITIVE_KEY_MARKERS: tuple[str, ...] = (
    "authorization", "cookie", "token", "secret", "password", "passwd",
    "api_key", "apikey", "api-key", "x-api-key", "credential", "auth",
)

#: Argument keys that name a resource the action will touch. Rendered in full for
#: the operator (a path or URL is exactly what must be reviewable), never redacted.
_RESOURCE_KEY_MARKERS: tuple[str, ...] = (
    "path", "filepath", "filename", "file", "directory", "folder", "folder_path",
    "destination", "source", "url", "target", "host", "save_path", "code",
    "command",
)

_MAX_RESOURCE_RENDER = 512


def _is_sensitive(key: str) -> bool:
    k = key.lower()
    return any(m in k for m in _SENSITIVE_KEY_MARKERS)


def _canonical_json(obj) -> str:
    """Deterministic JSON for digesting: sorted keys, no whitespace jitter."""
    try:
        return json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str,
                          separators=(",", ":"))
    except Exception:
        return repr(obj)


def call_digest(tool: str, effective_call: dict) -> str:
    """A stable digest over the EFFECTIVE call — the identity the approval binds to.

    Computed over the tool name plus the effective arguments (defaults applied,
    reserved/override keys already stripped by the caller). Any divergence in any
    argument — including one that appears only after the 200th character — changes
    this digest, which is what makes the binding sound.
    """
    material = _canonical_json({"tool": tool, "args": effective_call})
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


#: Query-parameter / assignment names whose VALUE is a secret embedded inside an
#: otherwise-renderable string (a URL or a command). Round-1 F2: redaction was
#: key-NAME-based, so a secret carried inside a `url`/`code`/`command` VALUE (e.g.
#: `https://user:pw@h/?api_key=sk-...`) rendered verbatim — the "safe to log"
#: property was false for value-embedded secrets ("body-blindness by field name").
_EMBEDDED_SECRET_PARAMS: tuple[str, ...] = (
    "api_key", "apikey", "api-key", "access_token", "access-token", "token",
    "secret", "password", "passwd", "pwd", "key", "sig", "signature",
    "auth", "session", "sessionid", "x-api-key",
)
_URL_USERINFO_RE = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)[^/@\s]*@")
_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&#;]|\b)((?:" + "|".join(re.escape(p) for p in _EMBEDDED_SECRET_PARAMS)
    + r")\s*[=:]\s*)(\"?)([^&\s#;\"']+)")
#: Round-2 F1: the HTTP auth-header form is `scheme<SPACE>token`, NOT `key=value`,
#: so the query/kv matcher never fired on `Authorization: Bearer <tok>` — the most
#: common value-embedded credential. Match the header value (quoted or not) and any
#: bare `Bearer <tok>`, and any `<...token/secret/key/password...>: <value>` header.
_AUTH_HEADER_RE = re.compile(
    r"(?i)\b((?:proxy-)?authorization)(\s*[:=]\s*\"?)(bearer|basic|digest|negotiate|token)(\s+)(\S+)")
_BEARER_RE = re.compile(r"(?i)\b(bearer)(\s+)([A-Za-z0-9._~+/=-]{8,})")
_CRED_HEADER_RE = re.compile(
    r"(?i)\b([\w-]*(?:token|secret|password|passwd|apikey|api[-_]?key)[\w-]*)"
    r"(\s*[:=]\s*\"?)([^&\s#;\"']+)")


def _scrub_embedded_secrets(value: str) -> str:
    """Remove credentials embedded INSIDE a renderable value: URL userinfo
    (`scheme://user:pass@` → `scheme://`), secret-shaped `key=value`/`key:value`
    pairs, HTTP auth headers (`Authorization: Bearer <tok>`, `Basic <b64>`), bare
    bearer tokens, and any `*token*/*secret*/*key*: value` header. The operator
    still sees the destination and THAT a secret is present, never the secret."""
    if not isinstance(value, str) or not value:
        return value
    scrubbed = _URL_USERINFO_RE.sub(lambda m: m.group("scheme") + "<redacted>@", value)
    scrubbed = _AUTH_HEADER_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}{m.group(4)}<redacted>", scrubbed)
    scrubbed = _BEARER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>", scrubbed)
    scrubbed = _CRED_HEADER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}<redacted>",
                                   scrubbed)
    scrubbed = _QUERY_SECRET_RE.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}<redacted>", scrubbed)
    return scrubbed


def _redact_structure(obj):
    """Recursively make a value render-safe: a nested dict key that is sensitive
    (Round-2 F1: `headers={'Authorization': ...}`) has its value replaced with a
    size marker, and every string leaf is run through `_scrub_embedded_secrets`.
    Lists/tuples are walked. Non-container leaves are returned scrubbed."""
    if isinstance(obj, dict):
        red: dict = {}
        for k, v in obj.items():
            if isinstance(k, str) and _is_sensitive(k):
                raw = v if isinstance(v, str) else _canonical_json(v)
                red[k] = f"<redacted:{len(raw.encode('utf-8'))} bytes>"
            else:
                red[k] = _redact_structure(v)
        return red
    if isinstance(obj, (list, tuple)):
        return [_redact_structure(x) for x in obj]
    if isinstance(obj, str):
        return _scrub_embedded_secrets(obj)
    return obj


def _redacted_summary(tool_input: dict) -> dict:
    """Argument summary safe to render and to log: secrets become size markers,
    resources are shown in full (bounded), everything else is length-summarised."""
    out: dict = {}
    for k, v in sorted(tool_input.items()):
        if _is_sensitive(k):
            raw = v if isinstance(v, str) else _canonical_json(v)
            out[k] = f"<redacted:{len(raw.encode('utf-8'))} bytes>"
            continue
        # Round-2 F1: a non-sensitive key (e.g. "headers") may nest a sensitive
        # one; redact the structure recursively BEFORE serialising so a nested
        # Authorization/Cookie value never renders.
        sval = (_scrub_embedded_secrets(v) if isinstance(v, str)
                else _canonical_json(_redact_structure(v)))
        kl = k.lower()
        if any(m in kl for m in _RESOURCE_KEY_MARKERS):
            out[k] = sval[:_MAX_RESOURCE_RENDER] + (
                f"…(+{len(sval) - _MAX_RESOURCE_RENDER})" if len(sval) > _MAX_RESOURCE_RENDER else "")
        else:
            out[k] = sval if len(sval) <= 80 else f"<{len(sval)} chars>"
    return out


def _target_resources(tool_input: dict) -> list[str]:
    """The concrete resources (paths/URLs/hosts) this action will touch. Rendered
    in full so the operator reviews the destination, not a prefix of the dict."""
    targets: list[str] = []
    for k, v in sorted(tool_input.items()):
        kl = k.lower()
        if _is_sensitive(k):
            continue
        if any(m in kl for m in _RESOURCE_KEY_MARKERS) and isinstance(v, str) and v.strip():
            targets.append(f"{k}={_scrub_embedded_secrets(v)[:_MAX_RESOURCE_RENDER]}")
    return targets


@dataclass
class ToolApprovalDescriptor:
    tool: str
    risk_class: str
    operation: str
    call_digest: str
    payload_bytes: int
    payload_digest: str
    target_resources: list[str] = field(default_factory=list)
    redacted_summary: dict = field(default_factory=dict)
    rollback_hint: str = ""

    def render_preview(self) -> str:
        """The operator-facing descriptor. Surfaces the security-relevant fields —
        every target resource, the payload size and a digest of the whole payload —
        NOT a truncated prefix. A dangerous difference after char 200 changes a
        target and the digest, so it is visible here."""
        lines = [
            f"tool={self.tool} risk={self.risk_class} op={self.operation}",
            f"call_digest={self.call_digest[:16]} payload={self.payload_bytes}B "
            f"digest={self.payload_digest[:16]}",
        ]
        if self.target_resources:
            lines.append("targets: " + " | ".join(self.target_resources))
        if self.redacted_summary:
            lines.append("args: " + _canonical_json(self.redacted_summary))
        if self.rollback_hint:
            lines.append("rollback: " + self.rollback_hint)
        return "\n      ".join(lines)

    def to_dict(self) -> dict:
        return {
            "tool": self.tool,
            "risk_class": self.risk_class,
            "operation": self.operation,
            "call_digest": self.call_digest,
            "payload_bytes": self.payload_bytes,
            "payload_digest": self.payload_digest,
            "target_resources": list(self.target_resources),
            "redacted_summary": dict(self.redacted_summary),
            "rollback_hint": self.rollback_hint,
        }


def describe(tool: str, risk_class: str, tool_input: dict, effective_call: dict,
             *, operation: str = "invoke", rollback_hint: str = "") -> ToolApprovalDescriptor:
    payload = _canonical_json(tool_input)
    payload_bytes = len(payload.encode("utf-8"))
    payload_digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return ToolApprovalDescriptor(
        tool=tool,
        risk_class=risk_class,
        operation=operation,
        call_digest=call_digest(tool, effective_call),
        payload_bytes=payload_bytes,
        payload_digest=payload_digest,
        target_resources=_target_resources(tool_input),
        redacted_summary=_redacted_summary(tool_input),
        rollback_hint=rollback_hint,
    )


def bind_matches(descriptor: ToolApprovalDescriptor, tool: str,
                 effective_call: dict) -> bool:
    """§19 binding check: does the call about to run match the reviewed one?

    Recomputes the effective-call digest and compares it to the descriptor that
    the operator approved. A mismatch means the input was mutated between review
    and execution — approval does not carry to a different action."""
    return descriptor.call_digest == call_digest(tool, effective_call)
