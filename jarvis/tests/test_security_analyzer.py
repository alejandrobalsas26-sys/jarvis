"""
tests/test_security_analyzer.py — V64 M13 Code & Query Security Analyzer.

Mission-required coverage: vulnerable SQL concatenation detected, parameterized
query NOT falsely flagged, command injection, path traversal, SSRF, dangerous
subprocess, prompt-injection sink, and populated confidence/evidence/data-flow/
remediation/regression-test fields. Plus the SQLi eval dataset scored through the
M14 harness (cross-milestone integration).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from core.eval_harness import EvalRunner, load_cases, security_analyzer_eval_target
from core.security_analyzer import (
    SecurityAnalyzer,
    Severity,
    VulnCategory,
    analyze_code,
    format_report,
)

_SQLI_SET = Path(__file__).resolve().parents[1] / "evals" / "sql_injection" / "sqli.jsonl"


def _cats(code: str) -> set[str]:
    return {f.category.value for f in analyze_code(code)}


# ── SQL injection ─────────────────────────────────────────────────────────────
def test_sql_concatenation_detected():
    findings = analyze_code('cursor.execute("SELECT * FROM users WHERE id = " + user_id)')
    assert any(f.category is VulnCategory.SQL_INJECTION for f in findings)
    f = findings[0]
    assert f.cwe == "CWE-89"
    assert f.confidence > 0 and f.evidence and f.data_flow and f.remediation and f.regression_test


def test_sql_fstring_format_percent_detected():
    assert VulnCategory.SQL_INJECTION.value in _cats('cursor.execute(f"SELECT * FROM t WHERE n={n}")')
    assert VulnCategory.SQL_INJECTION.value in _cats('cursor.execute("SELECT * FROM t WHERE n={}".format(n))')
    assert VulnCategory.SQL_INJECTION.value in _cats('cursor.execute("SELECT * FROM t WHERE n=%s" % n)')


def test_sqlalchemy_text_misuse_detected():
    assert VulnCategory.SQL_INJECTION.value in _cats('db.execute(text("SELECT * FROM t WHERE n = " + name))')


def test_second_order_two_line_concat_detected():
    code = 'q = "SELECT * FROM t WHERE u = " + stored\ncursor.execute(q)'
    assert VulnCategory.SQL_INJECTION.value in _cats(code)


def test_parameterized_query_not_flagged():
    assert _cats('cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))') == set()


def test_orm_filter_not_flagged():
    assert _cats('session.query(User).filter(User.id == user_id).all()') == set()


def test_static_and_benign_sql_string_not_flagged():
    assert _cats('cursor.execute("SELECT id FROM users")') == set()
    assert _cats('msg = "use SELECT to query the table"') == set()


def test_tainted_sql_is_critical_untainted_dynamic_is_high():
    tainted = analyze_code('cursor.execute("SELECT * FROM t WHERE u = " + request.args["u"])')
    assert tainted[0].severity is Severity.CRITICAL
    dynamic = analyze_code('cursor.execute("SELECT * FROM t WHERE u = " + local_var)')
    assert dynamic[0].severity is Severity.HIGH


# ── command injection / subprocess ────────────────────────────────────────────
def test_shell_true_subprocess_detected():
    assert VulnCategory.INSECURE_SUBPROCESS.value in _cats("import subprocess\nsubprocess.run(cmd, shell=True)")


def test_argv_subprocess_not_flagged():
    assert _cats('import subprocess\nsubprocess.run(["nmap", "-sV", target], shell=False)') == set()


def test_os_system_dynamic_detected():
    assert VulnCategory.COMMAND_INJECTION.value in _cats('import os\nos.system("ping " + host)')


# ── other families ────────────────────────────────────────────────────────────
def test_dynamic_exec_detected_literal_safe():
    assert VulnCategory.DYNAMIC_CODE_EXECUTION.value in _cats("eval(user_input)")
    assert _cats('eval("2 + 2")') == set()


def test_unsafe_deserialization_detected_and_safe_yaml_ignored():
    assert VulnCategory.UNSAFE_DESERIALIZATION.value in _cats("import pickle\npickle.loads(data)")
    assert VulnCategory.UNSAFE_DESERIALIZATION.value in _cats("import yaml\nyaml.load(f)")
    assert _cats("import yaml\nyaml.safe_load(f)") == set()


def test_ssrf_detected_tainted_only():
    assert VulnCategory.SSRF.value in _cats('import requests\nrequests.get(request.args["url"])')
    assert _cats('import requests\nrequests.get("https://api.example.com")') == set()


def test_path_traversal_detected():
    assert VulnCategory.PATH_TRAVERSAL.value in _cats('open(request.args["file"])')
    assert _cats('open("/etc/hosts")') == set()


def test_template_injection_detected():
    assert VulnCategory.TEMPLATE_INJECTION.value in _cats('render_template_string(request.args["t"])')


def test_weak_crypto_detected():
    assert VulnCategory.WEAK_CRYPTO.value in _cats("import hashlib\nhashlib.md5(pw).hexdigest()")


def test_hardcoded_secret_detected_placeholder_ignored():
    assert VulnCategory.CREDENTIAL_LEAKAGE.value in _cats('api_key = "sk-live-abc123def456"')
    assert _cats('api_key = "your_api_key"') == set()


def test_prompt_injection_sink_detected():
    code = 'page = fetch(request.args["url"])\nllm.chat(page)'
    # page is tainted via request.args → llm.chat sink
    assert VulnCategory.PROMPT_INJECTION_SINK.value in _cats(code)


# ── robustness ────────────────────────────────────────────────────────────────
def test_syntax_error_returns_no_findings():
    assert analyze_code("def broken(:\n  pass") == []


def test_findings_are_deterministically_ordered_and_capped():
    code = "\n".join('cursor.execute("SELECT * FROM t WHERE x = " + v%d)' % i for i in range(5))
    f1 = analyze_code(code)
    f2 = analyze_code(code)
    assert [x.line for x in f1] == [x.line for x in f2]  # deterministic
    assert all(f1[i].line <= f1[i + 1].line for i in range(len(f1) - 1))


def test_format_report_smoke():
    findings = analyze_code('cursor.execute("SELECT * FROM t WHERE id = " + uid)')
    rep = format_report(findings)
    assert "sql_injection" in rep and "fix:" in rep
    assert format_report([]) == "No security findings."


def test_analyzer_findings_dict_shape():
    d = SecurityAnalyzer().analyze_findings_dict('eval(x)')
    assert d and set(d[0]) >= {"category", "severity", "confidence", "line", "data_flow", "remediation"}


# ── SQLi eval dataset through the M14 harness ─────────────────────────────────
def test_sqli_eval_dataset_all_pass_through_harness():
    cases = load_cases(_SQLI_SET)
    assert len(cases) >= 10
    run = asyncio.run(EvalRunner(security_analyzer_eval_target()).run_suite(cases, run_id="sqli", now_ts=1.0))
    assert run.pass_rate == 1.0, [(r.case_id, r.failures) for r in run.results if not r.passed]
