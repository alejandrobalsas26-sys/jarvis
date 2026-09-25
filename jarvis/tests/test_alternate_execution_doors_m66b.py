"""tests/test_alternate_execution_doors_m66b.py — V69 M66B (§3/§4/§5).

The recovery audit found a SECOND arbitrary-code door: run_shell_command and
RedTeamShellExecutor.execute_shell could run `python attacker.py`, `python -m
evil`, `node a.js`, `npm run evil` — arbitrary caller-controlled code on the HOST
with only RESTRICTED_ONLY authority (HITL/NATO are authority, not L3 containment).

M66B closes it: the host command gateway is now EXPLICITLY INCAPABLE of
arbitrary code (interpreters may run only informational flags), and arbitrary
code goes through code_execute → the SANDBOX_REQUIRED ContainmentBroker.

The final invariant: EVERY caller-controlled arbitrary-code path is either
BROKER_REQUIRED or explicitly incapable of arbitrary code.
"""
from __future__ import annotations

import pytest

from tools.executor import (
    _validate_command,
    _forbidden_interpreter_exec,
    _resolve_within_allowed,
)
from core.execution_surface_registry import (
    ARBITRARY_CODE_SURFACES,
    BROKER_REQUIRED,
    RESTRICTED_ONLY,
    coverage,
    restricted_only_surfaces,
)
from scripts.check_execution_surfaces import scan_source


# ── §4: the gateway is explicitly incapable of arbitrary code ─────────────────
# Interpreters IN the base allowlist: these are refused by the M66B interpreter
# block specifically (message points at code_execute). Others (nodejs/ruby/…) are
# refused earlier by the allowlist; both are blocked, the door is closed either way.
@pytest.mark.parametrize("cmd", [
    "python attacker.py",
    "python3 attacker.py",
    "python -m http.server",
    "python -mattacker",
    "node a.js",
    "npm run evil",
    "make",              # bare make runs the default Makefile target
    "make evil-target",
    "python",            # bare interpreter (interactive) is refused too
    "node",
])
def test_allowlisted_interpreter_arbitrary_code_is_blocked_by_m66b(cmd):
    ok, msg, _ = _validate_command(cmd)
    assert not ok, f"{cmd!r} was allowed — arbitrary-code door still open"
    assert "arbitrary code execution" in msg or "code_execute" in msg


@pytest.mark.parametrize("cmd", [
    "nodejs a.js", "npx something", "ruby a.rb", "perl a.pl", "php a.php",
    "bash a.sh", "sh a.sh", "pwsh -File a.ps1",
])
def test_other_interpreters_are_blocked_too(cmd):
    # Not in the base allowlist ⇒ blocked at the allowlist stage; still no
    # arbitrary-code door.
    ok, _, _ = _validate_command(cmd)
    assert not ok, f"{cmd!r} was allowed"


@pytest.mark.parametrize("cmd", [
    "python --version",
    "python3 --version",
    "node --version",
    "nmap --version",
    "git status",
    "git log",
    "ls -la",
    "ping -c 1 8.8.8.8",
    "whois example.com",
    "ps aux",
])
def test_fixed_and_informational_commands_still_pass(cmd):
    ok, _, _ = _validate_command(cmd)
    assert ok, f"{cmd!r} was wrongly blocked — legitimate fixed command"


def test_helper_flags_script_and_module_forms():
    assert _forbidden_interpreter_exec(["python", "x.py"]) is not None
    assert _forbidden_interpreter_exec(["python", "-c", "print(1)"]) is not None
    assert _forbidden_interpreter_exec(["python", "-m", "evil"]) is not None
    assert _forbidden_interpreter_exec(["make"]) is not None
    assert _forbidden_interpreter_exec(["python", "--version"]) is None
    assert _forbidden_interpreter_exec(["nmap", "-sV", "t"]) is None   # not an interpreter


def test_lab_allowlist_does_not_reopen_the_door():
    # Even with the trusted-lab extra allowlist, an interpreter cannot run code.
    from tools.executor import _LAB_COMMAND_ALLOWLIST
    ok, _, _ = _validate_command("python attacker.py", _LAB_COMMAND_ALLOWLIST)
    assert not ok
    # …but a lab binary (not an interpreter) still validates under lab mode.
    ok2, _, _ = _validate_command("masscan 1.2.3.4", _LAB_COMMAND_ALLOWLIST)
    assert ok2


# ── §4 invariant: arbitrary-code ⇒ BROKER_REQUIRED; else explicitly incapable ─
def test_every_arbitrary_surface_is_broker_required():
    for name, d in ARBITRARY_CODE_SURFACES.items():
        if d["arbitrary_code"]:
            assert d["disposition"] == BROKER_REQUIRED, (
                f"{name} is arbitrary-code but not BROKER_REQUIRED")


def test_shell_gateways_are_restricted_and_incapable():
    for name in ("run_shell_command", "red_team_shell"):
        d = ARBITRARY_CODE_SURFACES[name]
        assert d["arbitrary_code"] is False
        assert d["disposition"] == RESTRICTED_ONLY
    assert {"run_shell_command", "red_team_shell"} <= restricted_only_surfaces()


def test_coverage_invariant_after_closure():
    arbitrary, covered = coverage()
    assert arbitrary == covered == frozenset({"code_execute"})


# ── §5: the static detector still flags a NEW arbitrary interpreter launch ────
_NEW_INTERPRETER_LAUNCH = '''
class ToolExecutor:
    def _tool_run_user_script(self, path):
        import subprocess
        return subprocess.run(["python", path])
'''

_FIXED_INTERNAL = '''
class ToolExecutor:
    def _tool_network_scan(self, target):
        import subprocess
        return subprocess.run(["nmap", "-sV", target], shell=False)
'''


def test_detector_flags_new_interpreter_launch_outside_broker():
    findings = scan_source(_NEW_INTERPRETER_LAUNCH, "tools/executor.py")
    assert findings, "a new subprocess interpreter launch was not flagged"


def test_detector_allows_reviewed_fixed_internal_launch():
    assert scan_source(_FIXED_INTERNAL, "tools/executor.py") == []


# ── §6 (Round-6, fresh independent review): a THIRD alternate door — a
# write_file target under a VCS metadata directory (.git/.svn/.hg). `git
# status` (an already-allowed, argv-innocuous base command) executes whatever
# `core.fsmonitor` names in `.git/config`; neither host-gateway shell executor
# overrides `subprocess.run`'s inherited cwd, and `_sandbox_allowed_dirs()`
# includes `Path.cwd()`, so a write_file call and a later, individually
# innocent-looking `git status` — no -c, no suspicious argv at all — chain
# into arbitrary host code with nothing suspicious in either HITL approval.
# `_git_reason` cannot see this: it only ever inspects argv, never file
# contents. Closed structurally in `_resolve_within_allowed`, the one shared
# containment gate every path-taking handler (read_file, write_file, …) uses,
# rather than by trying to enumerate every hook/config key a VCS might read.
def test_resolve_within_allowed_refuses_git_config_relative():
    assert _resolve_within_allowed(".git/config") is None


def test_resolve_within_allowed_refuses_git_hooks():
    assert _resolve_within_allowed(".git/hooks/pre-commit") is None


def test_resolve_within_allowed_refuses_git_metadata_under_every_allowed_root():
    import os
    home = os.path.expanduser("~")
    for root in ("Downloads", "Documents"):
        assert _resolve_within_allowed(
            os.path.join(home, root, "proj", ".git", "config")) is None


def test_resolve_within_allowed_refuses_svn_and_hg_too():
    assert _resolve_within_allowed(".svn/entries") is None
    assert _resolve_within_allowed(".hg/hgrc") is None


def test_resolve_within_allowed_still_allows_a_normal_file():
    # The fix must not become a blanket denial — only VCS metadata paths.
    resolved = _resolve_within_allowed("m66b_round6_ordinary_file.txt")
    assert resolved is not None
    assert ".git" not in resolved.parts


def test_msfconsole_c_config_is_blocked():
    from core.command_policy import command_refusal
    assert command_refusal(["msfconsole", "-c", "evil.yml"], lab=True) is not None
    assert command_refusal(["msfconsole", "-cevil.yml"], lab=True) is not None
    assert command_refusal(["msfconsole", "--config=evil.yml"], lab=True) is not None


# ── Round-8 (fresh independent review of the gen46 frozen candidate): the
# Round-5/6 comments here and in test_command_policy_m66b.py both asserted
# -M/--migration-path is "different from -m and legitimate" — TRUE that it's a
# different flag, WRONG that it's safe. It was never independently checked.
# Traced through the installed msfconsole's Ruby source: -M's directory is
# appended to migrations_paths and every pending .rb file under it is LOADED
# (top-level code runs unconditionally — standard ActiveRecord/Rails
# behavior) at ordinary console startup, via `framework.db` being touched
# with no db_migrate command needed. Confirmed live with a real throwaway
# Postgres cluster and a planted migration file.
def test_msfconsole_migration_path_is_blocked():
    from core.command_policy import command_refusal
    assert command_refusal(["msfconsole", "-M", "evildir"], lab=True) is not None
    assert command_refusal(["msfconsole", "--migration-path", "evildir"],
                            lab=True) is not None
    assert command_refusal(["msfconsole", "-Mevildir"], lab=True) is not None
    assert command_refusal(["msfconsole", "--migration-path=evildir"],
                            lab=True) is not None


# ── Round-10 (fresh independent review of the gen48 frozen candidate): the R4-R9
# msfconsole deniers matched long options with `typed.startswith(canonical)`,
# which only catches the FULL long form. msfconsole parses argv with Ruby's
# OptionParser, which accepts any UNAMBIGUOUS PREFIX — so `--plug`/`--reso`/
# `--migr`/`--module-p`/`--conf`/`--exec` all resolved to their dangerous
# option and slipped past the deniers (verified live against the bundled ruby
# using the real option definitions). `-x`'s long alias `--execute-command`
# was also missing entirely. Direct-call (§8 non-vacuity: asserts the
# command_policy layer itself refuses, not some earlier gate).
def test_msfconsole_optionparser_abbreviations_are_blocked():
    from core.command_policy import command_refusal
    for argv in (
        ["msfconsole", "--execute-command", "X"],   # -x long alias (was missing)
        ["msfconsole", "--exec", "X"],              # abbreviation of it
        ["msfconsole", "--plug", "f"],              # --plugin
        ["msfconsole", "--plu", "f"],
        ["msfconsole", "--module-p", "d"],          # --module-path
        ["msfconsole", "--migr", "d"],              # --migration-path
        ["msfconsole", "--reso", "f"],              # --resource
        ["msfconsole", "--conf", "y"],              # --config
    ):
        assert command_refusal(argv, lab=True) is not None, argv


def test_msfconsole_legitimate_abbreviations_still_allowed():
    # The prefix-of-canonical direction must not over-block safe options whose
    # own (usable, unambiguous) abbreviations are NOT prefixes of a dangerous
    # option name. --module-count / --no-defer-module-loads / --environment
    # (and its --env abbreviation) / --yaml all stay allowed.
    from core.command_policy import command_refusal
    for argv in (
        ["msfconsole", "--module-count"],
        ["msfconsole", "--no-defer-module-loads", "-q"],
        ["msfconsole", "--environment", "production"],
        ["msfconsole", "--env", "production"],
        ["msfconsole", "--yaml", "db.yml"],
    ):
        assert command_refusal(argv, lab=True) is None, argv


# ── Round-11 (fresh independent review of the gen49 frozen candidate): ffuf's
# `-config <file>` loads a TOML config that can set `inputcommands`/`inputshell`
# — the same host-shell-command capability `-input-cmd`/`-input-shell` are
# refused for. A slash-free argv (`ffuf -config evilrc`) clears the §G path
# guard and the config body is never argv-inspected, so the refusal must be at
# the config-load flag itself. Direct-call (§8 non-vacuity).
def test_ffuf_config_load_is_blocked():
    from core.command_policy import command_refusal
    for argv in (
        ["ffuf", "-config", "evilrc"],
        ["ffuf", "-config=evilrc"],
        ["ffuf", "--config", "evilrc"],
        ["ffuf", "--config=evilrc"],
    ):
        assert command_refusal(argv, lab=True) is not None, argv


def test_ffuf_ordinary_fuzzing_still_allowed():
    from core.command_policy import command_refusal
    for argv in (
        ["ffuf", "-u", "t", "-w", "words.txt"],
        ["ffuf", "-u", "t", "-mc", "200", "-t", "40"],
    ):
        assert command_refusal(argv, lab=True) is None, argv


# ── Round-12 (fresh independent review of the gen50 frozen candidate): two
# "spelling the parser accepts" gaps. (1) nmap's getopt_long treats a single
# leading dash for long options identically, so `-script=`/`-datadir` reached
# the NSE Lua engine (os.execute/io.popen) while only the `--` spellings were
# refused — and nmap is a BASE gateway binary, needing no trusted-lab mode.
# (2) sqlmap's `--eval`/`--alert` were full-token-matched while the SAME
# function's tamper/preprocess/postprocess check was already abbreviation-safe;
# argparse accepts the unambiguous `--eva`/`--ale`. Direct-call (§8
# non-vacuity), plus the over-block guards that keep ordinary scanning working.
def test_nmap_nse_blocked_in_every_dash_spelling():
    from core.command_policy import command_refusal
    for argv in (
        ["nmap", "-script=x.nse", "127.0.0.1"],
        ["nmap", "-script", "x.nse", "127.0.0.1"],
        ["nmap", "--script=x.nse", "127.0.0.1"],
        ["nmap", "-script-args=a=b"],
        ["nmap", "-script-trace"],
        ["nmap", "-datadir", "d"],
        ["nmap", "--datadi", "d"],
    ):
        assert command_refusal(argv, lab=False) is not None, argv


def test_nmap_ordinary_scan_flags_still_allowed():
    # -sC/-sV/-sn run only nmap's OWN bundled scripts/probes (its declared
    # capability); -d/-dd are debug. None may be caught by the NSE denier.
    from core.command_policy import command_refusal
    for argv in (
        ["nmap", "-sV", "-sC", "127.0.0.1"],
        ["nmap", "-sn", "127.0.0.1"],
        ["nmap", "-A", "-T4", "127.0.0.1"],
        ["nmap", "-d", "127.0.0.1"],
        ["nmap", "-dd", "-v", "127.0.0.1"],
    ):
        assert command_refusal(argv, lab=False) is None, argv


def test_sqlmap_eval_alert_abbreviations_blocked():
    from core.command_policy import command_refusal
    for argv in (
        ["sqlmap", "--eva=X"],
        ["sqlmap", "--ale=X"],
        ["sqlmap", "--eval", "X"],
        ["sqlmap", "--alert=X"],
    ):
        assert command_refusal(argv, lab=True) is not None, argv


def test_sqlmap_unrelated_options_still_allowed():
    # --all/--eta/--exclude-sysdbs share a leading letter with alert/eval but
    # are not prefixes of them; they must stay allowed.
    from core.command_policy import command_refusal
    for argv in (
        ["sqlmap", "-u", "t", "--all", "--batch"],
        ["sqlmap", "-u", "t", "--eta"],
        ["sqlmap", "-u", "t", "--exclude-sysdbs"],
    ):
        assert command_refusal(argv, lab=True) is None, argv
