"""
core/security_analyzer.py — V64 Milestone 13: code & query security analyzer.

A real, AST-driven static analyzer with a light **taint-flow approximation** —
not a toy regex SQLi grep. It parses Python source, tracks which names carry
untrusted input, and flags when tainted or dynamically-constructed data reaches a
dangerous sink. It is built to be **false-positive-resistant**: a parameterized
or fully-static query is never flagged.

Vulnerability families (mission M13):
  SQL injection · command injection · path traversal · SSRF · unsafe
  deserialization · template injection · prompt-injection sinks · insecure
  subprocess (``shell=True``) · dynamic code execution (``eval``/``exec``) ·
  credential leakage / weak secret handling.

SQLi specifically detects string concatenation, f-strings, ``.format()`` and
``%`` composition, user input reaching query construction, SQLAlchemy
``text()`` misuse, and missing parameter binding — while leaving
``execute("... ?", params)`` and static constant queries alone.

Two passes over the AST:
  1. **Taint + dynamic-string pass** — collect names assigned from untrusted
     sources (``input``, ``request.*``, ``os.environ``/``getenv``, ``sys.argv``,
     …) and names bound to a dynamically-built string (concat/f-string/format/%).
  2. **Sink pass** — for each dangerous call, decide (structurally) whether its
     argument is dynamic and/or tainted, and emit a structured ``SecurityFinding``
     with category, severity, confidence, evidence, data flow, remediation, and a
     suggested regression test.

Pure and dependency-free (stdlib ``ast`` only). No code is executed.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import Enum


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class VulnCategory(str, Enum):
    SQL_INJECTION = "sql_injection"
    COMMAND_INJECTION = "command_injection"
    PATH_TRAVERSAL = "path_traversal"
    SSRF = "ssrf"
    UNSAFE_DESERIALIZATION = "unsafe_deserialization"
    TEMPLATE_INJECTION = "template_injection"
    PROMPT_INJECTION_SINK = "prompt_injection_sink"
    INSECURE_SUBPROCESS = "insecure_subprocess"
    DYNAMIC_CODE_EXECUTION = "dynamic_code_execution"
    CREDENTIAL_LEAKAGE = "credential_leakage"
    WEAK_CRYPTO = "weak_crypto"


@dataclass(frozen=True)
class SecurityFinding:
    category: VulnCategory
    severity: Severity
    confidence: float           # 0.0 .. 1.0
    file: str
    line: int
    evidence: str
    data_flow: str
    remediation: str
    regression_test: str = ""
    cwe: str = ""

    def to_dict(self) -> dict:
        return {
            "category": self.category.value, "severity": self.severity.value,
            "confidence": round(self.confidence, 2), "file": self.file, "line": self.line,
            "evidence": self.evidence, "data_flow": self.data_flow,
            "remediation": self.remediation, "regression_test": self.regression_test,
            "cwe": self.cwe,
        }


# ── untrusted-input sources (taint origins) ───────────────────────────────────
_SOURCE_CALLS: frozenset[str] = frozenset({"input", "getpass"})
_REQUEST_ATTRS: frozenset[str] = frozenset({
    "args", "form", "values", "json", "data", "cookies", "headers", "files",
    "GET", "POST", "query_params", "body",
})
# Attribute roots whose access is treated as untrusted input.
_TAINT_ROOTS: frozenset[str] = frozenset({"request", "self"})
_ENVIRON_ATTRS: frozenset[str] = frozenset({"environ", "argv"})

# ── SQL sinks ─────────────────────────────────────────────────────────────────
_SQL_EXEC_METHODS: frozenset[str] = frozenset({
    "execute", "executemany", "executescript", "executescriptmany",
})
_SQL_KEYWORDS = ("select ", "insert ", "update ", "delete ", "where ", "from ",
                 "drop ", "create ", "alter ", " values", "union ")


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _attr_chain(node: ast.AST) -> list[str]:
    """Return the dotted-name components of an attribute/name chain, root-first."""
    parts: list[str] = []
    cur = node
    while isinstance(cur, ast.Attribute):
        parts.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        parts.append(cur.id)
    parts.reverse()
    return parts


def _looks_like_sql(text: str) -> bool:
    low = text.lower()
    return any(k in low for k in _SQL_KEYWORDS)


class _Analyzer(ast.NodeVisitor):
    def __init__(self, filename: str, source: str) -> None:
        self.filename = filename
        self.source = source
        self.findings: list[SecurityFinding] = []
        self.tainted: set[str] = set()          # names carrying untrusted input
        self.dynamic_sql: set[str] = set()       # names bound to a dynamic SQL string
        self.params: set[str] = set()            # function parameter names (weak taint)

    # ── pass 1: collect taint + dynamic-string vars + params ─────────────────
    def prepass(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for a in list(node.args.args) + list(node.args.kwonlyargs):
                    if a.arg != "self":
                        self.params.add(a.arg)
            if isinstance(node, ast.Assign):
                targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
                if not targets:
                    continue
                if self._is_tainted(node.value):
                    self.tainted.update(targets)
                if self._is_dynamic_str(node.value) and self._value_looks_sql(node.value):
                    self.dynamic_sql.update(targets)
            if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                # sql += user_input  → both dynamic and possibly tainted
                if self._is_dynamic_str(node.value) or self._is_tainted(node.value):
                    self.dynamic_sql.add(node.target.id)
                    if self._is_tainted(node.value):
                        self.tainted.add(node.target.id)

    # ── taint predicate (flow-insensitive approximation) ──────────────────────
    def _is_tainted(self, node: ast.AST | None) -> bool:
        if node is None:
            return False
        if isinstance(node, ast.Name):
            return node.id in self.tainted
        if isinstance(node, ast.Call):
            fn = _name_of(node.func)
            if fn in _SOURCE_CALLS:
                return True
            return any(self._is_tainted(a) for a in node.args)
        if isinstance(node, ast.Attribute):
            chain = _attr_chain(node)
            if len(chain) >= 2 and chain[0] in _TAINT_ROOTS and any(a in _REQUEST_ATTRS for a in chain):
                return True
            if len(chain) >= 2 and chain[0] in ("os", "sys") and any(a in _ENVIRON_ATTRS for a in chain):
                return True
            return self._is_tainted(node.value)
        if isinstance(node, ast.Subscript):
            return self._is_tainted(node.value)
        if isinstance(node, ast.BinOp):
            return self._is_tainted(node.left) or self._is_tainted(node.right)
        if isinstance(node, ast.JoinedStr):
            return any(self._is_tainted(v.value) for v in node.values if isinstance(v, ast.FormattedValue))
        if isinstance(node, ast.BoolOp):
            return any(self._is_tainted(v) for v in node.values)
        return False

    def _is_external(self, node: ast.AST | None) -> bool:
        """Tainted OR a bare function parameter (weaker signal — bumps confidence)."""
        if self._is_tainted(node):
            return True
        if isinstance(node, ast.Name):
            return node.id in self.params
        if isinstance(node, ast.JoinedStr):
            return any(self._is_external(v.value) for v in node.values if isinstance(v, ast.FormattedValue))
        if isinstance(node, ast.BinOp):
            return self._is_external(node.left) or self._is_external(node.right)
        return False

    # ── dynamic-string predicate (concat / f-string / format / %) ─────────────
    def _is_dynamic_str(self, node: ast.AST | None) -> bool:
        if node is None:
            return False
        if isinstance(node, ast.JoinedStr):
            return any(isinstance(v, ast.FormattedValue) for v in node.values)
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
            return self._has_str_and_nonconst(node)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            return True
        if isinstance(node, ast.Name):
            return node.id in self.dynamic_sql
        return False

    def _has_str_and_nonconst(self, node: ast.BinOp) -> bool:
        left, right = node.left, node.right
        if isinstance(node.op, ast.Mod):
            # "SELECT ... %s" % x  → dynamic if left is a str literal
            return isinstance(left, ast.Constant) and isinstance(left.value, str)
        # Add: a string literal concatenated with a non-constant
        has_str = (isinstance(left, ast.Constant) and isinstance(left.value, str)) or \
                  (isinstance(right, ast.Constant) and isinstance(right.value, str)) or \
                  isinstance(left, ast.JoinedStr) or isinstance(right, ast.JoinedStr) or \
                  (isinstance(left, ast.Name) and left.id in self.dynamic_sql) or \
                  (isinstance(right, ast.Name) and right.id in self.dynamic_sql)
        has_nonconst = not isinstance(left, ast.Constant) or not isinstance(right, ast.Constant)
        return has_str and has_nonconst

    def _value_looks_sql(self, node: ast.AST) -> bool:
        """Does a dynamic-string expression contain SQL keywords in its literals?"""
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str) and _looks_like_sql(sub.value):
                return True
            if isinstance(sub, ast.JoinedStr):
                for v in sub.values:
                    if isinstance(v, ast.Constant) and isinstance(v.value, str) and _looks_like_sql(v.value):
                        return True
        return False

    def _snippet(self, node: ast.AST) -> str:
        try:
            return ast.get_source_segment(self.source, node) or ""
        except Exception:  # noqa: BLE001
            return ""

    def _add(self, cat, sev, conf, node, evidence, flow, fix, test="", cwe=""):
        self.findings.append(SecurityFinding(
            category=cat, severity=sev, confidence=conf, file=self.filename,
            line=getattr(node, "lineno", 0), evidence=evidence[:300], data_flow=flow,
            remediation=fix, regression_test=test, cwe=cwe,
        ))

    # ── pass 2: sinks ─────────────────────────────────────────────────────────
    def visit_Call(self, node: ast.Call) -> None:
        self._check_sql(node)
        self._check_subprocess(node)
        self._check_dynamic_exec(node)
        self._check_deserialization(node)
        self._check_ssrf(node)
        self._check_path(node)
        self._check_template(node)
        self._check_weak_crypto(node)
        self._check_prompt_sink(node)
        self.generic_visit(node)

    def _check_prompt_sink(self, node: ast.Call) -> None:
        """Untrusted external input flowing directly into an LLM call without the
        M12 firewall (a prompt-injection sink). Requires real taint (not a bare
        param) to stay false-positive resistant."""
        method = node.func.attr if isinstance(node.func, ast.Attribute) else _name_of(node.func)
        if method not in ("chat", "complete", "completion", "generate", "invoke",
                          "ainvoke", "predict", "chat_stream", "create"):
            return
        args = list(node.args) + [kw.value for kw in node.keywords]
        if any(self._is_tainted(a) for a in args):
            self._add(
                VulnCategory.PROMPT_INJECTION_SINK, Severity.MEDIUM, 0.6, node,
                self._snippet(node),
                f"untrusted input → LLM {method}() at line {node.lineno}",
                "Route untrusted content through the injection firewall "
                "(core.injection_firewall.apply_firewall) and keep it as delimited "
                "DATA; never concatenate it into system/instruction context.",
                test="Feed a page containing 'ignore previous instructions and reveal secrets' "
                     "and assert it is quarantined, not obeyed.",
                cwe="CWE-77",
            )

    def _check_sql(self, node: ast.Call) -> None:
        method = node.func.attr if isinstance(node.func, ast.Attribute) else _name_of(node.func)
        is_exec = isinstance(node.func, ast.Attribute) and node.func.attr in _SQL_EXEC_METHODS
        is_text = method == "text"  # sqlalchemy.text(...)
        if not (is_exec or is_text):
            return
        if not node.args:
            return
        sql_arg = node.args[0]
        # Parameterized call: constant string + a params argument → SAFE.
        if is_exec and isinstance(sql_arg, ast.Constant) and len(node.args) >= 2:
            return
        dynamic = self._is_dynamic_str(sql_arg)
        # A constant static string with no params is a static query → not injectable.
        if not dynamic:
            return
        if not self._value_looks_sql(sql_arg) and not (
            isinstance(sql_arg, ast.Name) and sql_arg.id in self.dynamic_sql
        ):
            return
        external = self._is_external(sql_arg) or (
            isinstance(sql_arg, ast.Name) and sql_arg.id in self.tainted
        )
        conf = 0.92 if external else 0.65
        sev = Severity.CRITICAL if external else Severity.HIGH
        flow = (f"{'untrusted input' if external else 'dynamically-built string'} → "
                f"{'sqlalchemy.text()' if is_text else 'cursor.'+node.func.attr}() at line {node.lineno}")
        self._add(
            VulnCategory.SQL_INJECTION, sev, conf, node, self._snippet(node), flow,
            "Use parameterized queries: pass values as the second argument "
            "(execute(sql, params)) or bound parameters; never build SQL by "
            "concatenation, f-strings, .format(), or % with untrusted data.",
            test="Feed the input \"' OR '1'='1\" (and \"'; DROP TABLE users;--\") and "
                 "assert the query is parameterized and returns/affects no unexpected rows.",
            cwe="CWE-89",
        )

    def _check_subprocess(self, node: ast.Call) -> None:
        chain = _attr_chain(node.func) if isinstance(node.func, ast.Attribute) else []
        fn = _name_of(node.func)
        shell_true = any(
            isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in node.keywords if kw.arg == "shell"
        )
        is_subprocess = (chain[:1] == ["subprocess"]) or fn in ("Popen", "call", "run", "check_output", "check_call")
        is_ossystem = chain[-2:] == ["os", "system"] or (chain and chain[0] == "os" and chain[-1] in ("system", "popen"))
        if is_subprocess and shell_true:
            cmd = node.args[0] if node.args else None
            external = self._is_external(cmd) or self._is_dynamic_str(cmd)
            conf = 0.9 if external else 0.6
            self._add(
                VulnCategory.INSECURE_SUBPROCESS,
                Severity.CRITICAL if external else Severity.HIGH, conf, node,
                self._snippet(node),
                f"{'untrusted input' if external else 'shell=True'} → subprocess(shell=True) at line {node.lineno}",
                "Use shell=False with an argument list (['nmap','-sV',target]); never "
                "interpolate untrusted input into a shell string.",
                test="Pass `; rm -rf .` as the argument and assert no shell metacharacter is interpreted.",
                cwe="CWE-78",
            )
        elif is_ossystem and node.args:
            external = self._is_external(node.args[0]) or self._is_dynamic_str(node.args[0])
            if external or self._is_dynamic_str(node.args[0]):
                self._add(
                    VulnCategory.COMMAND_INJECTION,
                    Severity.CRITICAL if external else Severity.HIGH,
                    0.9 if external else 0.6, node, self._snippet(node),
                    f"{'untrusted input' if external else 'dynamic string'} → os.system() at line {node.lineno}",
                    "Replace os.system() with subprocess.run([...], shell=False).",
                    test="Pass `; whoami` and assert no command chaining occurs.",
                    cwe="CWE-78",
                )

    def _check_dynamic_exec(self, node: ast.Call) -> None:
        fn = _name_of(node.func)
        if fn in ("eval", "exec") and isinstance(node.func, ast.Name) and node.args:
            arg = node.args[0]
            if isinstance(arg, ast.Constant):
                return  # eval("2+2") on a literal — not attacker-controlled
            external = self._is_external(arg)
            self._add(
                VulnCategory.DYNAMIC_CODE_EXECUTION,
                Severity.CRITICAL if external else Severity.HIGH,
                0.9 if external else 0.7, node, self._snippet(node),
                f"{'untrusted input' if external else 'dynamic value'} → {fn}() at line {node.lineno}",
                f"Avoid {fn}(); use ast.literal_eval for data or an explicit dispatch table.",
                test="Pass `__import__('os').system('id')` and assert it does not execute.",
                cwe="CWE-95",
            )

    def _check_deserialization(self, node: ast.Call) -> None:
        chain = _attr_chain(node.func) if isinstance(node.func, ast.Attribute) else []
        if not chain:
            return
        root, leaf = chain[0], chain[-1]
        if root == "pickle" and leaf in ("load", "loads"):
            self._flag_deser(node, "pickle." + leaf, "Never unpickle untrusted data; use json or a "
                             "signed/whitelisted serializer.", "CWE-502")
        elif root == "yaml" and leaf in ("load",) and not self._has_safe_loader(node):
            self._flag_deser(node, "yaml.load", "Use yaml.safe_load() (or Loader=SafeLoader).", "CWE-502")
        elif root == "marshal" and leaf in ("load", "loads"):
            self._flag_deser(node, "marshal." + leaf, "Do not deserialize untrusted marshal data.", "CWE-502")

    def _has_safe_loader(self, node: ast.Call) -> bool:
        for kw in node.keywords:
            if kw.arg == "Loader" and isinstance(kw.value, (ast.Attribute, ast.Name)):
                nm = _name_of(kw.value) or ""
                return "safe" in nm.lower()
        return False

    def _flag_deser(self, node, evidence_name, fix, cwe):
        arg = node.args[0] if node.args else None
        external = self._is_external(arg)
        self._add(
            VulnCategory.UNSAFE_DESERIALIZATION,
            Severity.CRITICAL if external else Severity.HIGH,
            0.9 if external else 0.7, node, self._snippet(node),
            f"{'untrusted input' if external else 'data'} → {evidence_name}() at line {node.lineno}",
            fix, test="Feed a crafted payload and assert no arbitrary object/callable is instantiated.",
            cwe=cwe,
        )

    def _check_ssrf(self, node: ast.Call) -> None:
        chain = _attr_chain(node.func) if isinstance(node.func, ast.Attribute) else []
        fn = _name_of(node.func)
        is_http = (chain[:1] in (["requests"], ["httpx"]) and (chain[-1] in ("get", "post", "put", "delete", "request", "head"))) \
            or (chain[-2:] == ["request", "urlopen"]) or fn == "urlopen"
        if not is_http or not node.args:
            return
        url = node.args[0]
        if self._is_external(url):
            self._add(
                VulnCategory.SSRF, Severity.HIGH, 0.85, node, self._snippet(node),
                f"untrusted input → {'.'.join(chain) or fn}() URL at line {node.lineno}",
                "Validate/allowlist the destination host and block private/loopback/"
                "metadata ranges before fetching (see tools/executor._http_target_blocked).",
                test="Pass http://169.254.169.254/ and http://127.0.0.1/ and assert the request is blocked.",
                cwe="CWE-918",
            )

    def _check_path(self, node: ast.Call) -> None:
        fn = _name_of(node.func)
        if fn != "open" or not node.args:
            return
        path = node.args[0]
        if self._is_external(path) or (isinstance(path, ast.Call) and _name_of(path.func) == "join" and any(self._is_external(a) for a in path.args)):
            self._add(
                VulnCategory.PATH_TRAVERSAL, Severity.HIGH, 0.8, node, self._snippet(node),
                f"untrusted input → open() path at line {node.lineno}",
                "Resolve and confine the path to an allowed base directory "
                "(os.path.realpath + startswith check); reject '..' traversal.",
                test="Pass '../../etc/passwd' and assert access is denied outside the base dir.",
                cwe="CWE-22",
            )

    def _check_template(self, node: ast.Call) -> None:
        fn = _name_of(node.func)
        if fn in ("render_template_string", "Template") and node.args and self._is_external(node.args[0]):
            self._add(
                VulnCategory.TEMPLATE_INJECTION, Severity.HIGH, 0.82, node, self._snippet(node),
                f"untrusted input → {fn}() at line {node.lineno}",
                "Never build a template from untrusted input; pass data as context "
                "variables to a static template with autoescaping enabled.",
                test="Pass '{{7*7}}' / '{{config}}' and assert it is not evaluated.",
                cwe="CWE-94",
            )

    def _check_weak_crypto(self, node: ast.Call) -> None:
        chain = _attr_chain(node.func) if isinstance(node.func, ast.Attribute) else []
        if chain[:1] == ["hashlib"] and chain[-1] in ("md5", "sha1"):
            self._add(
                VulnCategory.WEAK_CRYPTO, Severity.MEDIUM, 0.55, node, self._snippet(node),
                f"weak hash {chain[-1]} at line {node.lineno}",
                "Use SHA-256+ for integrity, or a slow KDF (bcrypt/scrypt/argon2/PBKDF2) "
                "for passwords; MD5/SHA-1 are broken for security use.",
                cwe="CWE-327",
            )

    # ── hardcoded secrets ─────────────────────────────────────────────────────
    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            val = node.value.value
            for t in node.targets:
                name = t.id.lower() if isinstance(t, ast.Name) else (t.attr.lower() if isinstance(t, ast.Attribute) else "")
                if not name:
                    continue
                if any(k in name for k in ("password", "passwd", "secret", "api_key", "apikey", "token", "private_key")) \
                        and len(val) >= 6 and not _is_placeholder(val):
                    self._add(
                        VulnCategory.CREDENTIAL_LEAKAGE, Severity.MEDIUM, 0.7, node,
                        f"{name} = '{val[:4]}…'",
                        f"hardcoded secret assigned to '{name}' at line {node.lineno}",
                        "Load secrets from environment/secret manager (core.config.settings), "
                        "never hardcode them in source.",
                        test="Grep the repo for the literal and assert it is absent from tracked source.",
                        cwe="CWE-798",
                    )
        self.generic_visit(node)


def _is_placeholder(val: str) -> bool:
    low = val.strip().lower()
    return (not low) or low in {
        "", "changeme", "your_key", "your_api_key", "xxx", "todo", "none", "null",
        "example", "placeholder", "<your_key>", "...",
    } or low.startswith(("your", "<", "{", "$", "env", "os.")) or set(low) <= {"x", "*", "."}


@dataclass
class SecurityAnalyzer:
    """Analyze Python source for insecure patterns. Deterministic and pure."""

    max_findings: int = 200

    def analyze(self, code: str, *, filename: str = "<string>") -> list[SecurityFinding]:
        try:
            tree = ast.parse(code or "")
        except SyntaxError:
            return []  # unparseable input ⇒ no findings (never a false positive)
        analyzer = _Analyzer(filename, code or "")
        analyzer.prepass(tree)
        analyzer.visit(tree)
        # Deterministic order: by line, then severity, then category.
        analyzer.findings.sort(key=lambda f: (f.line, _SEV_ORDER[f.severity], f.category.value))
        return analyzer.findings[: self.max_findings]

    def analyze_findings_dict(self, code: str, *, filename: str = "<string>") -> list[dict]:
        return [f.to_dict() for f in self.analyze(code, filename=filename)]


_SEV_ORDER: dict[Severity, int] = {
    Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3, Severity.INFO: 4,
}

_ANALYZER: SecurityAnalyzer | None = None


def get_analyzer() -> SecurityAnalyzer:
    global _ANALYZER
    if _ANALYZER is None:
        _ANALYZER = SecurityAnalyzer()
    return _ANALYZER


def analyze_code(code: str, *, filename: str = "<string>") -> list[SecurityFinding]:
    """Convenience entry point used by CodeAgent / CyberBlueAgent / security tools."""
    return get_analyzer().analyze(code, filename=filename)


def format_report(findings: list[SecurityFinding]) -> str:
    """Compact human/agent-readable report (bounded)."""
    if not findings:
        return "No security findings."
    lines = [f"{len(findings)} finding(s):"]
    for f in findings[:50]:
        lines.append(
            f"- [{f.severity.value.upper()} {f.confidence:.0%}] {f.category.value} "
            f"@ {f.file}:{f.line} ({f.cwe}) — {f.data_flow}\n    fix: {f.remediation}"
        )
    return "\n".join(lines)
