"""V69 M68D — H03: secret-safe observability.

The invariant: the representation PRIVACY APPROVED must be the representation
EMITTED. Before M68D the order was backwards. ``output_summary`` was
``json.dumps(result)[:200]`` over the RAW result and went straight to the
forensic JSONL audit AND the AURA broadcast; ``_check_pii_output`` ran after it
and only ever ADDED a warning key.

MEASURED with synthetic canaries: a password and a Bearer token reached the audit
JSONL on both the sync and the async execution path, and the AURA payload. A
canary nested three levels down was absent — but only because it fell past the
200-character cut, and moving it to the front of the same result put it in the
file. Truncation was doing a redactor's job.

Every secret in this module is a SYNTHETIC canary invented here. No real
credential, no operator data.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import tokenize
from pathlib import Path

import pytest

import core.governance as gov
import core.memory_router as memory_router
import core.risk_classes as risk_classes
import tools.executor as ex
from core import safe_observability as so

_SRC = Path(ex.__file__).read_text(encoding="utf-8")
_LINES = _SRC.splitlines()


def _code_lines(src: str) -> list[str]:
    out = [""] * (len(src.splitlines()) + 2)
    prev = tokenize.ENCODING
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and prev in (
                tokenize.NEWLINE, tokenize.NL, tokenize.INDENT,
                tokenize.DEDENT, tokenize.ENCODING):
            prev = tok.type
            continue
        if tok.string.strip():
            out[tok.start[0]] += " " + tok.string
        prev = tok.type
    return out


_CODE_LINES = _code_lines(_SRC)
#: One whitespace-normalised stream of the module's CODE. Joining the per-line
#: lists leaves a double space wherever a statement wraps, and a detector token
#: written with single spaces then misses a call split across two lines — which
#: is exactly how a reintroduced raw dump would be formatted (measured: the
#: mutation campaign's `H03_async_summary_before_redaction` survived for that
#: reason alone).
_CODE = re.sub(r"\s+", " ", " ".join(_CODE_LINES))


def code_region(header: str) -> str:
    starts = [i for i, line in enumerate(_LINES) if header in line]
    assert starts, f"region header not found: {header!r}"
    start = starts[0]
    indent = len(_LINES[start]) - len(_LINES[start].lstrip())
    end = len(_LINES)
    for i in range(start + 1, len(_LINES)):
        line = _LINES[i]
        if not line.strip():
            continue
        cur = len(line) - len(line.lstrip())
        if cur <= indent and line.lstrip().startswith(("def ", "class ", "@", "#")):
            end = i
            break
    return " ".join(_CODE_LINES[start + 1:end + 1])


# ── the canaries ─────────────────────────────────────────────────────────────

CANARY_PW = "CANARY-PW-m68d-7f3a9b1c-SYNTHETIC"
CANARY_BEARER = "CANARY-BEARER-m68d-aaaabbbbccccddddeeee"
CANARY_APIKEY = "CANARYAPIKEY0123456789abcdef"
CANARY_NESTED = "CANARY-NESTED-m68d-deadbeefcafe"
CANARY_ERROR = "CANARY-ERROR-m68d-0f0f0f0f0f0f"
CANARY_COOKIE = "CANARY-COOKIE-m68d-5e5e5e5e"
CANARY_CONNSTR = "CANARY-CONNPW-m68d-9a9a9a9a"
CANARY_PEM = ("-----BEGIN RSA PRIVATE KEY-----\n"
              "CANARYPRIVATEKEYBODYm68d0123456789\n"
              "-----END RSA PRIVATE KEY-----")

#: (label, text that carries it, the canary that must not survive)
SECRET_SHAPES = [
    ("password assignment", f"password: {CANARY_PW}", CANARY_PW),
    ("json quoted password", f'{{"password": "{CANARY_PW}"}}', CANARY_PW),
    ("bearer token", f"Authorization: Bearer {CANARY_BEARER}", CANARY_BEARER),
    ("api key shaped", f"api_key={CANARY_APIKEY}", CANARY_APIKEY),
    ("secret_key name", f"secret_key = {CANARY_NESTED}", CANARY_NESTED),
    ("private key block", CANARY_PEM, "CANARYPRIVATEKEYBODYm68d0123456789"),
    ("session cookie", f"Set-Cookie: session={CANARY_COOKIE}", CANARY_COOKIE),
    ("connection string",
     f"postgres://admin:{CANARY_CONNSTR}@db.example.test/app", CANARY_CONNSTR),
    ("authorization header",
     f"authorization: Basic {CANARY_APIKEY}", CANARY_APIKEY),
    ("stripe style key", f"sk_live_{CANARY_APIKEY}", CANARY_APIKEY),
]


def _all_canaries() -> list[str]:
    return [canary for _label, _text, canary in SECRET_SHAPES]


# ── the sanitizer ────────────────────────────────────────────────────────────

class TestTheGovernedSanitizer:
    @pytest.mark.parametrize("label,text,canary",
                             SECRET_SHAPES, ids=[s[0] for s in SECRET_SHAPES])
    def test_every_secret_shape_is_redacted(self, label, text, canary):
        out = so.sanitize_text(text)
        assert canary not in out, f"{label} survived sanitization"
        assert so.REDACTED in out or so.REDACTION_FAILED in out

    @pytest.mark.parametrize("label,text,canary",
                             SECRET_SHAPES, ids=[s[0] for s in SECRET_SHAPES])
    def test_contains_secret_recognises_every_shape(self, label, text, canary):
        assert so.contains_secret(text) is True, f"{label} is not recognised"

    def test_it_is_a_superset_of_the_governed_credential_vocabulary(self):
        """§18: one vocabulary. The composition must never redact LESS.

        Anything `core.memory_router` — the repository's designated credential
        vocabulary — removes, this module removes too. Non-vacuity: the sample
        set is asserted to contain at least one string that vocabulary actually
        redacts, or 'no regression found' would prove nothing.
        """
        samples = [t for _l, t, _c in SECRET_SHAPES] + [
            "AKIAIOSFODNN7EXAMPLE", "ghp_CANARY0123456789abcdefghij",
            "xoxb-CANARY-0123456789", "sk-CANARY0123456789abcdef",
            f"token: {CANARY_BEARER}", f"contraseña: {CANARY_PW}",
        ]
        redacted_by_governed = [t for t in samples
                                if memory_router.redact_secrets(t) != t]
        assert redacted_by_governed, "non-vacuity: the sample set is useless"
        for text in redacted_by_governed:
            assert so.sanitize_text(text) != text, \
                f"the governed vocabulary redacts {text!r} and this module does not"

    def test_a_harmless_result_stays_useful(self):
        """Positive control: redaction must not destroy observability."""
        result = {"files_scanned": 42, "findings": [], "status": "ok",
                  "path": "/x/y/z.txt", "note": "nothing of interest"}
        out = so.sanitize(result)
        assert out == result
        summary = so.safe_summary(result)
        assert "files_scanned" in summary and "42" in summary
        assert so.REDACTED not in summary

    def test_structure_is_preserved_while_values_are_redacted(self):
        result = {"a": {"b": [{"password": CANARY_PW}, {"ok": 1}]}, "n": 3}
        out = so.sanitize(result)
        assert out["n"] == 3
        assert out["a"]["b"][1] == {"ok": 1}
        assert out["a"]["b"][0]["password"] == so.REDACTED

    @pytest.mark.parametrize("field", [
        "password", "Password", "PASSWORD", "api_key", "apiKey", "API-KEY",
        "token", "authorization", "Cookie", "private_key", "client_secret",
        "secret", "session_token", "connectionString",
    ])
    def test_a_secret_field_name_redacts_an_opaque_value(self, field):
        """What no pattern can do: an opaque value under a telling name."""
        out = so.sanitize({field: "ab"})
        assert out[field] == so.REDACTED, \
            f"{field} kept an opaque value a pattern cannot recognise"

    @pytest.mark.parametrize("value", [
        "x", 1, 1.5, True, None, b"bytes", ["a"], ("a",), {"k": "v"}, {1, 2},
    ])
    def test_every_supported_type_is_walked_without_raising(self, value):
        so.sanitize(value)                                  # must not raise

    def test_a_secret_in_a_tuple_or_set_is_redacted(self):
        out = so.sanitize((f"password: {CANARY_PW}",))
        assert CANARY_PW not in json.dumps(out)
        out = so.sanitize({f"password: {CANARY_PW}"})
        assert CANARY_PW not in json.dumps(out, default=str)

    def test_bytes_become_a_length_not_a_body(self):
        assert so.sanitize(b"password: " + CANARY_PW.encode()) == f"<{43} bytes>"

    def test_an_unvetted_object_becomes_its_type_name(self):
        class Carrier:
            def __repr__(self):
                return f"password: {CANARY_PW}"

        assert so.sanitize(Carrier()) == "<Carrier>"
        assert CANARY_PW not in so.safe_summary(Carrier())

    def test_a_hidden_reasoning_block_is_removed_with_its_contents(self):
        """M67A.1's prohibition, reused rather than reimplemented."""
        out = so.sanitize_text(f"before <think>password: {CANARY_PW}</think> after")
        assert CANARY_PW not in out
        assert "[REDACTED-REASONING]" in out


class TestFailClosed:
    def test_a_cycle_becomes_a_refusal_not_a_passthrough(self):
        cycle: dict = {"secret": CANARY_PW}
        cycle["self"] = cycle
        out = so.sanitize(cycle)
        assert out["_redaction"] == so.REDACTION_FAILED
        assert CANARY_PW not in json.dumps(out)

    def test_excessive_nesting_becomes_a_refusal(self):
        deep: object = f"password: {CANARY_PW}"
        for _ in range(40):
            deep = {"next": deep}
        out = so.sanitize(deep)
        assert CANARY_PW not in json.dumps(out)

    def test_a_raising_sanitizer_never_falls_back_to_raw(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("synthetic sanitizer failure")

        monkeypatch.setattr(so, "_walk", boom)
        out = so.sanitize({"password": CANARY_PW})
        assert out["_redaction"] == so.REDACTION_FAILED
        assert CANARY_PW not in json.dumps(out)

    def test_a_raising_sanitizer_never_falls_back_to_raw_in_a_summary(self,
                                                                     monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("synthetic sanitizer failure")

        monkeypatch.setattr(so, "sanitize", boom)
        summary = so.safe_summary({"password": CANARY_PW})
        assert so.REDACTION_FAILED in summary
        assert CANARY_PW not in summary

    def test_a_raising_governed_pipeline_never_falls_back_to_raw(self, monkeypatch):
        def boom(*a, **kw):
            raise RuntimeError("synthetic pipeline failure")

        monkeypatch.setattr(so.redaction_policy, "redact_text", boom)
        out = so.sanitize_text(f"password: {CANARY_PW}")
        assert out == so.REDACTION_FAILED
        assert CANARY_PW not in out

    def test_an_unserialisable_value_never_leaks_through_the_summary(self):
        class Weird:
            def __repr__(self):
                return f"password: {CANARY_PW}"

        summary = so.safe_summary({"x": Weird()})
        assert CANARY_PW not in summary


class TestOrdering:
    def test_redaction_happens_before_truncation(self):
        """A secret past the cut is not 'safe', it is lucky.

        MEASURED: the nested canary was absent from the audit file only because
        it fell past 200 characters; moving it to the front put it in the file.
        """
        padded = {"pad": "x" * 500, "password": CANARY_PW}
        summary = so.safe_summary(padded, limit=200)
        assert len(summary) <= 200
        full = so.safe_summary(padded, limit=100_000)
        assert CANARY_PW not in full, "the secret only survived the short summary"
        assert so.REDACTED in full

    def test_redaction_happens_before_summarisation(self):
        result = {"password": CANARY_PW}
        assert CANARY_PW not in so.safe_summary(result)
        assert CANARY_PW not in json.dumps(so.sanitize(result))

    def test_a_secret_at_every_offset_is_redacted(self):
        for pad in (0, 50, 199, 200, 201, 1000):
            result = {"pad": "y" * pad, "password": CANARY_PW}
            assert CANARY_PW not in so.safe_summary(result, limit=100_000), \
                f"leaked at pad={pad}"


# ── the sinks, end to end ────────────────────────────────────────────────────

@pytest.fixture
def audit_sink(tmp_path, monkeypatch):
    """Point the persistent audit JSONL at a temporary file."""
    monkeypatch.setattr(gov, "_LOG_DIR", tmp_path)
    monkeypatch.setattr(gov, "_LOG_FILE", tmp_path / "tactic_audit.jsonl")
    return tmp_path / "tactic_audit.jsonl"


@pytest.fixture
def broadcasts(monkeypatch):
    captured: list[dict] = []

    async def fake(event):
        captured.append(so.sanitize(event))

    monkeypatch.setattr(ex, "_aura_broadcast", fake)
    return captured


@pytest.fixture
def canary_tool(monkeypatch):
    """A READ_ONLY tool whose result carries every canary shape."""
    monkeypatch.setitem(risk_classes.TOOL_RISK_CLASS, "m68d_canary",
                        risk_classes.RiskClass.READ_ONLY)
    payload = {
        "stdout": f"password: {CANARY_PW}",
        "headers": {"authorization": f"Bearer {CANARY_BEARER}"},
        "nested": {"deep": [{"secret_key": CANARY_NESTED}]},
        "api_key": CANARY_APIKEY,
        "rows": 7,
    }
    return payload


class TestSinksReceiveNoSecret:
    @staticmethod
    def _run_sync(payload, audit_sink, reasoning="[THINKING] decided to read"):
        executor = ex.ToolExecutor()
        executor._tool_m68d_canary = lambda **kw: dict(payload)   # type: ignore
        result = executor.execute("m68d_canary", {}, reasoning=reasoning)
        executor._audit.close()
        raw = audit_sink.read_text(encoding="utf-8")
        records = [json.loads(line) for line in raw.splitlines() if line.strip()]
        return result, raw, records

    def test_the_audit_jsonl_received_an_event_at_all(self, audit_sink, canary_tool):
        """NON-VACUITY. 'the file is empty, therefore no leak' is not a test."""
        _result, raw, records = self._run_sync(canary_tool, audit_sink)
        assert len(records) == 1, f"the sink received {len(records)} events"
        assert records[0]["tool"] == "m68d_canary"
        assert records[0]["auth_audit"] == "sync"
        assert records[0]["result"], "the sink recorded no result at all"
        assert raw.strip(), "the audit file is empty"

    @pytest.mark.parametrize("canary", [
        CANARY_PW, CANARY_BEARER, CANARY_NESTED, CANARY_APIKEY,
    ])
    def test_no_canary_reaches_the_audit_jsonl_sync_path(self, audit_sink,
                                                         canary_tool, canary):
        _result, raw, _records = self._run_sync(canary_tool, audit_sink)
        assert canary not in raw

    def test_a_secret_only_in_the_reasoning_does_not_reach_the_audit(
            self, audit_sink, canary_tool):
        _result, raw, records = self._run_sync(
            {"rows": 1}, audit_sink,
            reasoning=f"[THINKING] I will use password: {CANARY_PW}")
        assert records, "non-vacuity: nothing was written"
        assert CANARY_PW not in raw

    def test_a_secret_only_in_an_error_does_not_reach_the_audit(self, audit_sink,
                                                               monkeypatch):
        monkeypatch.setitem(risk_classes.TOOL_RISK_CLASS, "m68d_canary",
                            risk_classes.RiskClass.READ_ONLY)
        executor = ex.ToolExecutor()
        executor._tool_m68d_canary = lambda **kw: {            # type: ignore
            "error": f"connection failed for postgres://u:{CANARY_ERROR}@h/db"}
        executor.execute("m68d_canary", {}, reasoning="r")
        executor._audit.close()
        raw = audit_sink.read_text(encoding="utf-8")
        assert raw.strip(), "non-vacuity: nothing was written"
        assert CANARY_ERROR not in raw

    def test_a_raising_handler_does_not_put_its_message_in_the_audit(self,
                                                                    audit_sink,
                                                                    monkeypatch):
        monkeypatch.setitem(risk_classes.TOOL_RISK_CLASS, "m68d_canary",
                            risk_classes.RiskClass.READ_ONLY)

        def boom(**kw):
            raise RuntimeError(f"password: {CANARY_PW}")

        executor = ex.ToolExecutor()
        executor._tool_m68d_canary = boom                      # type: ignore
        executor.execute("m68d_canary", {}, reasoning="r")
        executor._audit.close()
        raw = audit_sink.read_text(encoding="utf-8")
        assert raw.strip()
        assert CANARY_PW not in raw

    def test_no_canary_reaches_the_aura_broadcast(self, audit_sink, broadcasts,
                                                  canary_tool, monkeypatch):
        monkeypatch.setitem(risk_classes.TOOL_RISK_CLASS, "m68d_canary",
                            risk_classes.RiskClass.READ_ONLY)
        executor = ex.ToolExecutor()
        executor._tool_m68d_canary = lambda **kw: dict(canary_tool)  # type: ignore

        async def main():
            return await executor.aexecute("m68d_canary", {}, reasoning="r")

        asyncio.run(main())
        executor._audit.close()
        assert broadcasts, "NON-VACUITY: the AURA sink received nothing"
        blob = json.dumps(broadcasts, ensure_ascii=False, default=str)
        for canary in (CANARY_PW, CANARY_BEARER, CANARY_NESTED, CANARY_APIKEY):
            assert canary not in blob
        assert any(e.get("type") == "tool_result" for e in broadcasts)

    def test_no_canary_reaches_the_audit_on_the_async_path(self, audit_sink,
                                                           broadcasts,
                                                           canary_tool,
                                                           monkeypatch):
        monkeypatch.setitem(risk_classes.TOOL_RISK_CLASS, "m68d_canary",
                            risk_classes.RiskClass.READ_ONLY)
        executor = ex.ToolExecutor()
        executor._tool_m68d_canary = lambda **kw: dict(canary_tool)  # type: ignore

        async def main():
            return await executor.aexecute("m68d_canary", {}, reasoning="r")

        asyncio.run(main())
        executor._audit.close()
        raw = audit_sink.read_text(encoding="utf-8")
        assert raw.strip(), "NON-VACUITY: the audit sink received nothing"
        for canary in (CANARY_PW, CANARY_BEARER, CANARY_NESTED, CANARY_APIKEY):
            assert canary not in raw

    def test_the_aura_sink_sanitizes_whatever_a_call_site_hands_it(self):
        """The guarantee is at the SINK, so a future call site cannot bypass it."""
        captured: list[dict] = []

        async def fake_broadcast(event):
            captured.append(event)

        async def main():
            import aura.server as server_mod
            return server_mod

        # Patch the lazily imported broadcast and call the real sink.
        import sys
        import types
        stub = types.ModuleType("aura.server")
        stub.broadcast = fake_broadcast
        pkg = sys.modules.get("aura")
        created = False
        if pkg is None:
            pkg = types.ModuleType("aura")
            pkg.__path__ = []                                  # type: ignore
            sys.modules["aura"] = pkg
            created = True
        previous = sys.modules.get("aura.server")
        sys.modules["aura.server"] = stub
        try:
            asyncio.run(ex._aura_broadcast(
                {"type": "x", "message": f"password: {CANARY_PW}"}))
        finally:
            if previous is not None:
                sys.modules["aura.server"] = previous
            else:
                sys.modules.pop("aura.server", None)
            if created:
                sys.modules.pop("aura", None)
        assert captured, "NON-VACUITY: the sink was never reached"
        assert CANARY_PW not in json.dumps(captured, default=str)

    def test_the_audit_sink_sanitizes_whatever_a_caller_hands_it(self, audit_sink):
        """Sink-local: log_action called DIRECTLY with raw values still redacts."""
        logger = gov.TacticAuditLogger()
        logger.log_action("t", f"reasoning password: {CANARY_PW}", "sync", "success",
                          f"result api_key={CANARY_APIKEY}",
                          command=f"psql postgres://u:{CANARY_CONNSTR}@h/db",
                          resolved_path="/tmp/x", binary_status="allowlist_ok")
        logger.close()
        raw = audit_sink.read_text(encoding="utf-8")
        assert raw.strip(), "NON-VACUITY: nothing was written"
        for canary in (CANARY_PW, CANARY_APIKEY, CANARY_CONNSTR):
            assert canary not in raw

    def test_the_returned_result_is_deliberately_not_redacted(self, audit_sink,
                                                              canary_tool):
        """Scope, stated honestly.

        The caller ASKED for the file; redacting the value handed back would
        break the tool. What M68D guarantees is that no SINK keeps it. The
        warning key stays, because the caller should know.
        """
        result, _raw, _records = self._run_sync(canary_tool, audit_sink)
        assert CANARY_PW in json.dumps(result, default=str)
        assert "_pii_warning" in result


class TestLoguruSink:
    """The general log is a persistent sink too, and §18 names it.

    A handler's exception message reaches `logger.error` and from there the log
    file. An exception is as much a carrier as a result: a failed connection
    arrives as `str(e)` carrying the connection string.
    """

    def test_a_handler_exception_message_is_sanitized_before_logging(self,
                                                                     monkeypatch):
        from loguru import logger as _logger

        captured: list[str] = []
        sink_id = _logger.add(lambda msg: captured.append(str(msg)), level="ERROR")
        try:
            monkeypatch.setitem(risk_classes.TOOL_RISK_CLASS, "m68d_canary",
                                risk_classes.RiskClass.READ_ONLY)

            def boom(**kw):
                raise RuntimeError(
                    f"connect failed: postgres://u:{CANARY_ERROR}@h/db")

            executor = ex.ToolExecutor()
            executor._tool_m68d_canary = boom                  # type: ignore
            executor.execute("m68d_canary", {}, reasoning="r")
            executor._audit.close()
        finally:
            _logger.remove(sink_id)
        assert captured, "NON-VACUITY: the log sink received nothing"
        blob = "\n".join(captured)
        assert "Error en tool" in blob, "the expected record was not emitted"
        assert CANARY_ERROR not in blob

    def test_every_tool_error_log_site_uses_the_sanitizer(self):
        sites = [line for line in _LINES
                 if "logger.error(f\"Error en tool" in line]
        assert len(sites) >= 4, f"non-vacuity: only {len(sites)} sites found"
        joined = re.sub(r"\s+", " ", " ".join(_LINES))
        assert "logger.error(f\"Error en tool '{tool_name}': {e}\")" not in joined, \
            "a tool exception message is logged unsanitized"
        assert "logger.error(f\"Error en tool MCP '{tool_name}': {e}\")" not in joined


class TestLlmHistorySink:
    def test_tool_output_entering_the_prompt_history_is_sanitized(self):
        """The sink that persists longest. It used to be fail-open twice over."""
        body = Path(ex.__file__).parent.parent / "core" / "llm.py"
        src = body.read_text(encoding="utf-8")
        region = src.split("result_str = json.dumps(result, ensure_ascii=False)", 1)[1]
        region = region.split("labeled = self._label_tool_result", 1)[0]
        # Comments describe the REMOVED fail-open code, so a raw substring search
        # reads that history as live code (measured — it is why this strips first).
        code = "\n".join(line for line in region.splitlines()
                         if not line.strip().startswith("#"))
        assert "_safe_obs.sanitize_text(" in code
        assert "except Exception:" not in code, \
            "the history sink still swallows a redaction failure"
        assert "if self._context_mgr is not None" not in code, \
            "the history sink still depends on an optional redactor"


# ── absent-control ───────────────────────────────────────────────────────────

class TestAbsentControls:
    #: Every place a tool result becomes an observability string. Non-vacuity:
    #: asserted non-empty and each header asserted to exist.
    FAN_OUT_SITES = (
        "    async def aexecute(",
        "    def execute(",
    )

    def test_the_fan_out_inventory_is_not_vacuous(self):
        assert self.FAN_OUT_SITES
        for header in self.FAN_OUT_SITES:
            assert any(header in line for line in _LINES), f"missing {header}"

    @pytest.mark.parametrize("sample", [
        "x = json.dumps(result, ensure_ascii=False, default=str)[:200]\n",
        # The same call wrapped across two lines, which is how it comes back.
        "x = json.dumps(result, ensure_ascii=False,\n"
        "               default=str)[:200]\n",
    ])
    def test_the_raw_dump_detector_sees_both_formattings(self, sample):
        """Non-vacuity, and the reason the detector normalises whitespace."""
        token = "json . dumps ( result , ensure_ascii = False , default = str ) [ : 200 ]"
        normalised = re.sub(r"\s+", " ", " ".join(_code_lines(sample)))
        assert token in normalised, "detector is vacuous for this formatting"

    def test_no_sink_is_handed_a_raw_json_dump_of_a_result(self):
        """The exact pre-M68D composition must not exist anywhere."""
        token = "json . dumps ( result , ensure_ascii = False , default = str ) [ : 200 ]"
        assert token not in _CODE, \
            "a raw result dump is still being built for a sink"

    def test_every_output_summary_comes_from_the_governed_sanitizer(self):
        occurrences = [line for line in _LINES if "output_summary =" in line]
        assert occurrences, "non-vacuity: there are no summary sites to check"
        for line in occurrences:
            assert "safe_observability.safe_summary(" in line, \
                f"summary built without the sanitizer: {line.strip()}"

    def test_every_audit_call_site_passes_a_sanitized_summary(self):
        """The sync path builds its summary INSIDE the `log_action` call, so a
        line-scan for `output_summary =` cannot see it."""
        sites = [i for i, line in enumerate(_LINES)
                 if "self._audit.log_action(" in line]
        assert sites, "non-vacuity: there are no audit call sites"
        joined = re.sub(r"\s+", " ", " ".join(_LINES))
        assert "json.dumps(result, ensure_ascii=False, default=str)[:200]" \
            not in joined, "an audit call site serialises the raw result"

    def test_the_audit_sink_sanitizes_every_string_field_it_writes(self):
        src = (Path(ex.__file__).parent.parent / "core" / "governance.py").read_text(
            encoding="utf-8")
        record = src.split("record = {", 1)[1].split("}", 1)[0]
        for field in ("tool", "command", "resolved_path", "binary_status",
                      "auth_audit", "thinking", "result"):
            line = [ln for ln in record.splitlines() if f'"{field}":' in ln]
            assert line, f"the audit record no longer has a {field} field"
            assert "safe_observability." in line[0], \
                f"the audit {field} field is written unsanitized"

    def test_the_aura_sink_sanitizes_before_it_can_import_a_transport(self):
        region = code_region("async def _aura_broadcast(event: dict) -> None:")
        assert "safe_observability . sanitize ( event )" in region
        sanitize_at = region.index("safe_observability . sanitize ( event )")
        import_at = region.index("from aura . server import broadcast")
        assert sanitize_at < import_at, \
            "the payload is sanitized after the transport is resolved"
        assert "await broadcast ( safe_event )" in region, \
            "the sink broadcasts the raw event, not the sanitized one"

    def test_the_sanitizer_has_no_raw_fallback_branch(self):
        src = Path(so.__file__).read_text(encoding="utf-8")
        for fn in ("def sanitize(", "def safe_summary(", "def safe_reasoning(",
                   "def sanitize_text("):
            body = src.split(fn, 1)[1].split("\ndef ", 1)[0]
            assert "REDACTION_FAILED" in body, \
                f"{fn} has no fail-closed answer"
            assert "return value" not in body, f"{fn} can return its input raw"

    def test_the_pem_block_pattern_runs_before_the_governed_pipeline(self):
        """An ordering control, because the ordering is what the bug was.

        `memory_router` replaces the `-----BEGIN … PRIVATE KEY-----` delimiter
        with the marker. If the whole-block pattern runs after that, it has no
        anchor left and the key BODY survives — measured, by this suite.
        """
        assert so._PRE_SPANS, "non-vacuity: there is no pre-pass to check"
        assert any("PRIVATE KEY" in pattern.pattern for pattern in so._PRE_SPANS), \
            "the PEM block pattern is not in the pre-pass"
        assert not any("PRIVATE KEY" in pattern.pattern
                       for pattern in so._EXTRA_SPANS), \
            "the PEM pattern is in the post-pass, where its anchor is gone"
        src = Path(so.__file__).read_text(encoding="utf-8")
        body = src.split("def sanitize_text(", 1)[1].split("\ndef ", 1)[0]
        pre_at = body.index("_PRE_SPANS")
        governed_at = body.index("redaction_policy.redact_text(")
        post_at = body.index("_EXTRA_SPANS")
        assert pre_at < governed_at < post_at, \
            "the three passes are not in the order the measurement requires"

    def test_a_pem_body_never_survives_the_delimiter_being_redacted(self):
        block = ("-----BEGIN OPENSSH PRIVATE KEY-----\n"
                 "CANARYBODYSHOULDNOTSURVIVE0000\n"
                 "-----END OPENSSH PRIVATE KEY-----")
        out = so.sanitize_text(block)
        assert "CANARYBODYSHOULDNOTSURVIVE0000" not in out

    def test_an_unterminated_pem_block_is_cut_to_the_end(self):
        block = ("-----BEGIN RSA PRIVATE KEY-----\n"
                 "CANARYTRUNCATEDBODY0000000000")
        out = so.sanitize_text(block)
        assert "CANARYTRUNCATEDBODY0000000000" not in out

    def test_the_sanitizer_does_not_fork_the_credential_vocabulary(self):
        """§18: it must COMPOSE on the governed scanner, not replace it."""
        src = Path(so.__file__).read_text(encoding="utf-8")
        assert "from core import redaction_policy" in src
        assert "redaction_policy.redact_text(" in src
