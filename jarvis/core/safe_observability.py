"""
core/safe_observability.py — V69 M68D (H03): ONE sink-safe view of a tool result.

WHY THIS MODULE EXISTS
======================
A tool result is raw. It can hold a password a `cat` printed, an Authorization
header a fetch echoed back, a connection string an inventory tool returned.
Before M68D that raw value fanned out:

* ``output_summary`` was built from the RAW result with
  ``json.dumps(result)[:200]`` and handed to the forensic JSONL audit AND to the
  AURA broadcast;
* ``_check_pii_output`` ran AFTER that summary was built, and all it ever did was
  ADD a ``_pii_warning`` key — the secret itself stayed in the result;
* ``TacticAuditLogger.log_action`` wrote ``thinking`` and ``result`` to disk with
  no sink-local guarantee of its own.

MEASURED with synthetic canaries: a password and a Bearer token reached the audit
JSONL on both the sync and the async path, and the AURA broadcast payload, and
the ``reasoning`` field carried its canary to disk too. A canary nested three
levels down was absent — but only because it fell past the 200-character cut, and
moving it to the front of the same result put it in the file. Truncation was
doing the work a redactor was supposed to do.

THE RULE
========
::

    RAW OUTPUT IS NEVER FAN-OUT SAFE BY DEFAULT

    raw result
      -> sanitize (HERE)
      -> safe observability view
      -> audit JSONL / AURA / structured logs / history

Sanitization happens BEFORE summarisation and BEFORE truncation, because a
summary of a secret is still a secret and a truncated secret is a secret that
happened to be short.

FAIL CLOSED (§19)
=================
If sanitization raises, times out, or meets a structure it cannot walk, the
observability path gets :data:`REDACTION_FAILED` plus privacy-safe metadata —
never the raw body. An observability failure must not become a disclosure.

ONE VOCABULARY, NOT FIVE (§18)
==============================
This module does NOT start a third credential vocabulary. The repository already
designates ``core.redaction_policy`` as "the one deterministic redaction
scanner" for everything written to disk, and that module already delegates
credentials to ``core.memory_router``'s patterns "so there is one credential
vocabulary in the codebase, not two". So this module COMPOSES:

    sanitize_text = redaction_policy.redact_text   (the governed pipeline:
                                                    hidden reasoning, OTP,
                                                    credentials, home paths)
                  + _EXTRA_SPANS                   (shapes MEASURED to slip
                                                    through it, below)

and adds the capability nothing had: a STRUCTURED walk, and a rule that redacts
by FIELD NAME as well as by span.

``_EXTRA_SPANS`` is justified by measurement, not by taste. Of fifteen synthetic
shapes, the governed vocabulary redacted five and missed ten — including
``"password": "…"`` in JSON, which is the exact rendering a tool result carries
(``\b`` matches between ``password`` and the closing quote, but then ``[:=]``
cannot, so the pattern stops) and ``secret_key=…`` (``\bsecret\b`` cannot match
before ``_``). A superset test asserts the composition stays a superset: anything
``memory_router`` redacts, this module redacts too.

The structured layer is what catches what no pattern can: a field called
``password`` whose value is an opaque twelve-character string.

NOT A CHAIN-OF-THOUGHT CONCEPT (§21)
====================================
``reasoning`` is minimised rather than preserved. M67A.1's prohibition on hidden
reasoning stands: nothing here creates a private channel, and the audit keeps a
decision record, not conversational prose.
"""
from __future__ import annotations

import json
import re

from core import redaction_policy

__all__ = [
    "REDACTED",
    "REDACTION_FAILED",
    "SAFE_SUMMARY_LIMIT",
    "contains_secret",
    "is_secret_field",
    "safe_reasoning",
    "safe_summary",
    "sanitize",
    "sanitize_text",
]

#: The marker a redacted span becomes. One token, so a test can count them and a
#: reader can tell redaction from absence.
REDACTED = "[REDACTED-SECRET]"

#: What a sink receives when sanitization itself failed. It is NOT the raw body.
REDACTION_FAILED = "[REDACTION_FAILED]"

#: Default cut for a summary. Applied AFTER sanitization, never before.
SAFE_SUMMARY_LIMIT = 200

#: How deep the structured walk goes before it refuses to keep walking. A
#: self-referential or pathologically nested result must not become either a
#: crash or a raw passthrough — it becomes a refusal.
_MAX_DEPTH = 12

#: How many items of a sequence/mapping are walked. Beyond this the remainder is
#: summarised by COUNT, never by content.
_MAX_ITEMS = 2048

#: Length bound handed to the governed pipeline for a single string. Generous,
#: because the CALLER's own limit (`safe_summary`) is what shortens a record; this
#: one only stops a pathological single value from dominating the walk.
_TEXT_LIMIT = 8192

#: Field names whose VALUE is a credential whatever it looks like. A pattern
#: cannot recognise an opaque 12-character password, but a field called
#: ``password`` tells you what it is. Matched on the whole name, case-folded,
#: after stripping separators, so ``api_key``/``apiKey``/``API-KEY`` are one name.
_SECRET_FIELD_NAMES: frozenset[str] = frozenset({
    "password", "passwd", "pwd", "contraseña", "contrasena",
    "secret", "secretkey", "clientsecret", "apikey", "apisecret",
    "accesskey", "accesskeyid", "secretaccesskey",
    "token", "accesstoken", "refreshtoken", "idtoken", "authtoken",
    "sessiontoken", "bearertoken", "privatekey", "privkey",
    "authorization", "proxyauthorization", "cookie", "setcookie",
    "sessionid", "session", "credential", "credentials", "auth",
    "xapikey", "apitoken", "privatetoken", "vaulttoken", "dsn",
    "connectionstring", "connstr", "otp", "passphrase",
})

_NAME_SEPARATORS = re.compile(r"[^a-z0-9]+")

#: Span patterns for the shapes MEASURED to slip through the governed vocabulary
#: in `core.redaction_policy` / `core.memory_router`. Each one is here because a
#: synthetic canary of that shape survived that pipeline — not because it looked
#: like a good idea. See the module docstring for the measurement.
#: Patterns that must run BEFORE the governed pipeline, because that pipeline
#: consumes the anchor they depend on.
#:
#: MEASURED: `memory_router` matches the `-----BEGIN … PRIVATE KEY-----` DELIMITER
#: and replaces it with the marker. Run it first and the whole-block pattern has
#: no BEGIN line left to anchor on, so the key BODY survives on the next line.
#: This milestone's own test caught that; the external audit did not mention it.
_PRE_SPANS: tuple[re.Pattern, ...] = (
    # A PEM private key, WHOLE BLOCK.
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
               re.DOTALL),
    # An unterminated block: everything to the end of the string. Destructive by
    # design and consistent with how `redaction_policy` treats an unterminated
    # reasoning block — cutting to the end is the SAFE direction.
    re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*", re.DOTALL),
)

_EXTRA_SPANS: tuple[re.Pattern, ...] = (
    # A quoted value. `"password": "x"` — the governed pattern needs `[:=]`
    # immediately after the name and finds a quote instead.
    re.compile(
        r"""(?ix)
        ( \b(?: api[_-]?key | api[_-]?token | secret[_-]?key | secret
              | access[_-]?key[_-]?id | secret[_-]?access[_-]?key | access[_-]?key
              | client[_-]?secret | private[_-]?key | private[_-]?token
              | vault[_-]?token | auth[_-]?token | session[_-]?token
              | refresh[_-]?token | access[_-]?token | id[_-]?token
              | bearer[_-]?token | password | passwd | pwd | contrase\u00f1a
              | passphrase | credentials | credential
              | connection[_-]?string | conn[_-]?str | dsn | otp )\b
          \s* ["']? \s* [:=] (?! \s* ["']? \s* \[REDACTED ) \s* ["']? )
        ( (?! \[REDACTED ) [^\s"',}\]]+ )
        """),
    # Authorization / Proxy-Authorization / Cookie as a HEADER name. The value is
    # "scheme SPACE credential", so it must be taken to the END OF THE VALUE and
    # not to the first space: stopping at the space left `Basic <base64>` with the
    # base64 still in the clear (measured against this module's own first draft).
    re.compile(
        r"""(?ix)
        ( \b (?: proxy- )? authorization \b \s* ["']? \s* [:=]
          (?! \s* ["']? \s* \[REDACTED ) \s* ["']? )
        ( (?! \[REDACTED ) [^\n,}\]"']+ )
        """),
    re.compile(
        r"""(?ix)
        ( \b (?: set- )? cookie \b \s* ["']? \s* [:=]
          (?! \s* ["']? \s* \[REDACTED ) \s* ["']? )
        ( (?! \[REDACTED ) [^\n,}\]"']+ )
        """),
    # Credentials embedded in a URL authority: scheme://user:secret@host
    re.compile(r"(?i)(\b[a-z][a-z0-9+.\-]*://[^\s/:@]+:)((?!\[REDACTED)[^\s/@]+)(@)"),
    # Underscore-separated vendor keys; the governed pattern only covers `sk-`.
    re.compile(r"(?i)\b(?:sk|pk|rk)_(?:live|test|prod)_[A-Za-z0-9]{8,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}\b"),
)

#: Indices in :data:`_EXTRA_SPANS` whose match is (label)(secret)[(suffix)]: the
#: label is KEPT because ``password=[REDACTED-SECRET]`` is useful observability
#: while a bare marker is not. The rest replace their whole match.
_LABEL_PRESERVING = frozenset({0, 1, 2, 3})


def _normalized_name(name: object) -> str:
    return _NAME_SEPARATORS.sub("", str(name).strip().lower())


def is_secret_field(name: object) -> bool:
    """``True`` when a field by this name holds a credential by definition."""
    return _normalized_name(name) in _SECRET_FIELD_NAMES


def contains_secret(text: str) -> bool:
    """``True`` when *text* carries a span this module would redact."""
    if not text:
        return False
    if redaction_policy.redact_secrets(text) != text:
        return True
    return any(pattern.search(text)
               for pattern in _PRE_SPANS + _EXTRA_SPANS)


def sanitize_text(text: str, *, max_chars: int = _TEXT_LIMIT) -> str:
    """*text*, run through the governed pipeline and then the measured extras.

    The governed pipeline comes FIRST on purpose: it strips hidden-reasoning
    blocks before anything else, so a secret quoted only inside a ``<think>``
    block disappears with the block instead of surviving as a marker next to one.
    """
    if not text:
        return text
    out = str(text)
    for pattern in _PRE_SPANS:
        out = pattern.sub(REDACTED, out)
    try:
        out, _report = redaction_policy.redact_text(out, max_chars=max_chars)
    except Exception:                                          # noqa: BLE001
        return REDACTION_FAILED
    for index, pattern in enumerate(_EXTRA_SPANS):
        if index in _LABEL_PRESERVING and pattern.groups >= 2:
            if pattern.groups >= 3:
                out = pattern.sub(rf"\g<1>{REDACTED}\g<3>", out)
            else:
                out = pattern.sub(rf"\g<1>{REDACTED}", out)
        else:
            out = pattern.sub(REDACTED, out)
    return out


class _SanitizeRefused(Exception):
    """The structure could not be walked safely; the caller must fail closed."""


def _walk(value, depth: int, seen: set[int]):
    if depth > _MAX_DEPTH:
        raise _SanitizeRefused("structure deeper than the walk limit")
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, (bool, int, float, type(None))):
        return value
    if isinstance(value, bytes):
        # Bytes are not rendered: a length is observability, the body is not.
        return f"<{len(value)} bytes>"
    if isinstance(value, dict):
        if id(value) in seen:
            raise _SanitizeRefused("cycle in the result structure")
        seen = seen | {id(value)}
        out = {}
        for i, (key, item) in enumerate(value.items()):
            if i >= _MAX_ITEMS:
                out["_truncated_keys"] = len(value) - _MAX_ITEMS
                break
            safe_key = sanitize_text(str(key))
            if is_secret_field(key):
                # The NAME says what it is, so the value goes whatever it looks
                # like. This is what catches an opaque password a pattern cannot.
                out[safe_key] = REDACTED
            else:
                out[safe_key] = _walk(item, depth + 1, seen)
        return out
    if isinstance(value, (list, tuple, set, frozenset)):
        if id(value) in seen:
            raise _SanitizeRefused("cycle in the result structure")
        seen = seen | {id(value)}
        items = list(value)
        out_list = [_walk(item, depth + 1, seen) for item in items[:_MAX_ITEMS]]
        if len(items) > _MAX_ITEMS:
            out_list.append(f"<{len(items) - _MAX_ITEMS} more items>")
        return out_list
    # Anything else: describe the TYPE, never the repr. A repr is a body.
    return f"<{type(value).__name__}>"


def sanitize(value):
    """A sink-safe view of *value*, recursively.

    Structure is preserved where it is useful — keys stay keys, lists stay lists,
    numbers stay numbers — so the result is still worth logging. What does not
    survive is a credential: by field name, by span pattern, or by being an
    object whose repr nobody has vetted.

    Never raises. An unwalkable structure yields :data:`REDACTION_FAILED` plus a
    type name, which is the fail-closed answer rather than a passthrough.
    """
    try:
        return _walk(value, 0, set())
    except _SanitizeRefused as exc:
        return {"_redaction": REDACTION_FAILED, "_reason": str(exc)[:120],
                "_type": type(value).__name__}
    except Exception as exc:                                   # noqa: BLE001
        return {"_redaction": REDACTION_FAILED,
                "_reason": f"{type(exc).__name__}", "_type": type(value).__name__}


def safe_summary(value, limit: int = SAFE_SUMMARY_LIMIT) -> str:
    """A short, sink-safe string for *value*.

    ORDER IS THE POINT: sanitize, then serialise, then cut. The pre-M68D code
    serialised and cut the RAW value, so whether a secret leaked depended on how
    far into the JSON it happened to sit.
    """
    try:
        safe = sanitize(value)
        text = json.dumps(safe, ensure_ascii=False, default=lambda o: f"<{type(o).__name__}>")
    except Exception as exc:                                   # noqa: BLE001
        return f"{REDACTION_FAILED} ({type(exc).__name__})"
    if limit is not None and limit >= 0 and len(text) > limit:
        return text[:limit]
    return text


def safe_reasoning(reasoning: object, limit: int = SAFE_SUMMARY_LIMIT) -> str:
    """The minimised, sink-safe form of a turn's reasoning text (§21).

    Forensics needs to know a decision was made and by what authority; it does
    not need the prose. So this sanitizes and then CUTS HARD, and it is the only
    thing any sink is given. It does not create, and must not be read as, a
    hidden-reasoning channel.
    """
    if reasoning is None:
        return ""
    try:
        text = reasoning if isinstance(reasoning, str) else json.dumps(
            sanitize(reasoning), ensure_ascii=False,
            default=lambda o: f"<{type(o).__name__}>")
        safe = sanitize_text(text)
    except Exception as exc:                                   # noqa: BLE001
        return f"{REDACTION_FAILED} ({type(exc).__name__})"
    return safe[:limit] if limit is not None and limit >= 0 else safe
