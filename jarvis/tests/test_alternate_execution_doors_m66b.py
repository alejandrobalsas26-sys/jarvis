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

import re

import pytest

from tools.executor import (
    FILE_CAPABLE_TOOLS,
    FileIntent,
    ToolExecutor,
    _validate_command,
    _forbidden_interpreter_exec,
    _resolve_within_allowed,
)
from core.command_policy import command_refusal
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


# ── Round-13 (fresh independent review of the gen51 frozen candidate): four
# ABSENT controls — a flag family never enumerated, a parser spelling never
# covered, a config-indirect input never considered, and a second ungoverned
# argv surface. All four were live while the 113-mutation campaign passed,
# because every mutation weakens an EXISTING denier and none of these existed
# to be weakened. Direct-call (§8 non-vacuity) plus over-block guards.
def test_bare_token_is_not_an_info_flag_for_interpreters():
    # A bare non-option token is a script/goal name: `python3 version` and
    # `node version` run ./version, `make version` runs a cwd Makefile recipe
    # through /bin/sh — all reachable because write_file can plant that file in
    # Path.cwd(), which both shell gateways inherit.
    from tools.executor import _validate_command
    for cmd in ("python3 version", "python version", "node version",
                "make version", "python3 setup.py", "make all"):
        ok, _msg, _argv = _validate_command(cmd)
        assert not ok, cmd


def test_interpreter_info_flags_and_npm_version_still_allowed():
    # npm is the one binary for which a bare `version` is a subcommand.
    from tools.executor import _validate_command
    for cmd in ("python3 --version", "python -V", "node --version",
                "make --version", "npm --version", "npm version", "npm -v"):
        ok, msg, _argv = _validate_command(cmd)
        assert ok, cmd + " -> " + msg


def test_wget_getopt_long_abbreviations_are_blocked():
    # wget's getopt_long accepts any unambiguous prefix, so --conf=/--exec=/
    # --use-a= reached --config/--execute/--use-askpass (confirmed live); a
    # caller-authored wgetrc naming use_askpass runs a host program.
    from core.command_policy import command_refusal
    for argv in (["wget", "--conf=w"], ["wget", "--confi=w"], ["wget", "--config=w"],
                 ["wget", "--exec=x"], ["wget", "--execut=x"], ["wget", "-e", "x"],
                 ["wget", "--use-a=/h"], ["wget", "--use-ask=/h"], ["wget", "--use"]):
        assert command_refusal(argv) is not None, argv


def test_wget_legitimate_neighbours_still_allowed():
    # -c/--continue, --content-*, --exclude-*, --user* are NOT prefixes of a
    # denied name and must keep working.
    from core.command_policy import command_refusal
    for argv in (["wget", "-c", "http://t/f"], ["wget", "--continue", "http://t/f"],
                 ["wget", "--content-disposition", "http://t/f"],
                 ["wget", "--exclude-directories=/a", "http://t/f"],
                 ["wget", "--user=bob", "http://t/f"],
                 ["wget", "--user-agent=x", "http://t/f"]):
        assert command_refusal(argv) is None, argv


def test_nmap_resume_replay_is_blocked():
    # --resume replays a command line nmap parses OUT of the given file, so a
    # stored --script= reaches NSE without appearing in this argv.
    from core.command_policy import command_refusal
    for argv in (["nmap", "--resume", "r.txt"], ["nmap", "-resume", "r.txt"],
                 ["nmap", "--resum", "r.txt"], ["nmap", "--res", "r.txt"]):
        assert command_refusal(argv) is not None, argv
    # -r (consecutive ports) and --reason stay allowed.
    for argv in (["nmap", "-r", "127.0.0.1"], ["nmap", "--reason", "127.0.0.1"]):
        assert command_refusal(argv) is None, argv


def test_network_scan_scan_type_is_governed_by_command_policy():
    # A SECOND caller-controlled nmap argv surface: scan_type was only
    # metacharacter-screened and never reached the command-SEMANTIC policy.
    from tools.executor import ToolExecutor
    ex = ToolExecutor.__new__(ToolExecutor)
    for st in ("--script=pwn.nse", "-script pwn.nse", "--script-args x",
               "--datadir d", "--resume r.txt"):
        err = ex._tool_network_scan("127.0.0.1", st).get("error", "")
        assert "pol" in err, st + " -> " + err
    # An ordinary scan type must clear the policy (it may then fail for an
    # unrelated environmental reason, which is not a policy refusal).
    err = ex._tool_network_scan("127.0.0.1", "-sS -sV").get("error", "")
    assert "bloqueado por" not in err, err


# ── Round-14 (fresh independent review of the gen52 frozen candidate): two more
# ABSENT controls. (1) `git_query` was a THIRD caller-argv git surface and the
# most exposed one — RiskClass.READ_ONLY and HITL-EXEMPT, so zero operator
# approvals — whose only checks were a metacharacter screen and a short
# write-flag denylist. It never reached command_refusal, so `git log
# --output=<path> --format=%xNN…` was an arbitrary-path, arbitrary-content file
# write: enough to rewrite .git/config with a core.fsmonitor hook (host code on
# the next ordinary git command) without ever calling write_file, so the
# _VCS_METADATA_DIRS guard did not apply. (2) hashcat and john store their
# ORIGINAL argv in a session file and re-parse it on restore, so the
# session-REPLAY family reinstates flags the policy refuses — the same
# config-indirect class as nmap --resume.
def test_git_query_defers_to_the_command_policy():
    from tools.executor import ToolExecutor
    ex = ToolExecutor.__new__(ToolExecutor)
    # --output/-o is an arbitrary-path file write and is exactly what
    # _git_reason's banned_sub_opts refuses on the shell gateway.
    for args in ("--output=.git/config", "--output=/tmp/x", "-o /tmp/x",
                 "--format=%x41 --output=cfg"):
        err = ex._tool_git_query(operation="log", args=args).get("error", "")
        assert err, args
    # stash mutates the working tree and is outside the policy's git grammar,
    # so it is no longer an offered operation.
    assert ex._tool_git_query(operation="stash", args="clear").get("error")
    # destructive branch flags stay refused by the handler's own denylist
    for args in ("-D x", "-M y"):
        assert ex._tool_git_query(operation="branch", args=args).get("error"), args


def test_git_query_ordinary_read_only_queries_still_work():
    from tools.executor import ToolExecutor
    ex = ToolExecutor.__new__(ToolExecutor)
    for op, args in (("status", ""), ("log", "-n 3"), ("diff", ""),
                     ("branch", ""), ("show", "HEAD")):
        err = ex._tool_git_query(operation=op, args=args).get("error", "")
        assert "bloqueado por" not in err and "escritura" not in err, (op, args, err)


def test_hashcat_and_john_session_replay_is_blocked():
    from core.command_policy import command_refusal
    for argv in (["hashcat", "--restore"], ["hashcat", "--rest"], ["hashcat", "--res"],
                 ["hashcat", "--restore-file-path=f"], ["hashcat", "--restore-f=f"],
                 ["john", "--restore=p"], ["john", "--restore"], ["john", "--res=p"],
                 ["john", "--catch-up=p"], ["john", "--cat=p"],
                 ["john", "--status=p"], ["john", "--sta=p"]):
        assert command_refusal(argv, lab=True) is not None, argv


def test_hashcat_and_john_ordinary_cracking_still_allowed():
    # --restore-disable only SUPPRESSES the session file; --remove/--runtime and
    # john's --show/--session/--stdout are not prefixes of a denied name.
    from core.command_policy import command_refusal
    for argv in (["hashcat", "-m", "0", "h", "w"],
                 ["hashcat", "--restore-disable", "-m", "0", "h", "w"],
                 ["hashcat", "--remove", "-m", "0", "h", "w"],
                 ["hashcat", "--runtime=60", "h", "w"],
                 ["john", "hashfile"], ["john", "--wordlist=r.txt", "hashfile"],
                 ["john", "--show", "hashfile"], ["john", "--session=s", "hashfile"]):
        assert command_refusal(argv, lab=True) is None, argv


# ── Round-15 (fresh independent review of the gen53 frozen candidate): requiring
# a read flag to be PRESENT is not requiring a write action to be ABSENT. git's
# legacy `config --replace-all name value [value-pattern]` swallows a trailing
# `--list` as the optional value-pattern, so the write went through and .git/config
# gained a `core.fsmonitor` hook — the Round-6 primitive reached WITHOUT write_file
# (so _VCS_METADATA_DIRS never applied) and WITHOUT git_query (so Round-14's
# routing never applied). git config also abbreviates (`--rep` writes), and the
# other write actions were saved only by git's own argc rules, which is the tool
# saving us rather than the policy.
def test_git_config_write_actions_are_refused_with_any_read_flag_present():
    from tools.executor import _validate_command
    for cmd in ("git config --replace-all core.fsmonitor V --list",
                "git config --replace core.fsmonitor V --list",
                "git config --repl core.fsmonitor V --get",
                "git config --rep core.fsmonitor V -l",
                "git config --add core.fsmonitor V --list",
                "git config --unset core.filemode --list",
                "git config --unset-all core.filemode --get",
                "git config --remove-section core --list",
                "git config --rename-section a b --list",
                "git config -e --list",
                "git config --edit --list",
                "git config set core.fsmonitor V --list",
                "git config unset core.fsmonitor --list"):
        ok, _msg, _argv = _validate_command(cmd)
        assert not ok, cmd


def test_git_config_read_surface_is_intact():
    from tools.executor import _validate_command
    for cmd in ("git config --get user.name", "git config --list", "git config -l",
                "git config --get-all remote.origin.url",
                "git config --get-regexp ^user", "git config --local --list",
                "git config --show-origin --list",
                "git config --get --type=bool core.bare",
                "git config --list --name-only"):
        ok, msg, _argv = _validate_command(cmd)
        assert ok, cmd + " -> " + msg


def test_git_query_is_not_an_arbitrary_file_read_primitive():
    # `git diff --no-index <a> <b>` made this READ_ONLY, HITL-EXEMPT tool read
    # and print arbitrary host paths, bypassing _resolve_within_allowed — the one
    # centralized file-read gate. Absolute escapes are the unambiguous fixture; a
    # "../../" string's meaning depends on the CWD (see
    # test_read_file_sandbox_cwd.py) so it is deliberately not asserted here.
    from tools.executor import ToolExecutor
    ex = ToolExecutor.__new__(ToolExecutor)
    for args in ("--no-index /dev/null /etc/hostname", "--no-index=x /etc/hostname",
                 "/etc/hostname", "/etc/passwd"):
        assert ex._tool_git_query(operation="diff", args=args).get("error"), args
    for op, args in (("status", ""), ("log", "-n 3"), ("diff", ""),
                     ("show", "HEAD"), ("branch", "")):
        err = ex._tool_git_query(operation=op, args=args).get("error", "")
        assert not err, (op, args, err)


def test_firewall_rule_protocol_is_validated_before_interpolation():
    # proto/port are interpolated into a PowerShell -Command PROGRAM string, so
    # shell=False does not help: the element IS the program. A refused protocol
    # must return False without ever building or dispatching it.
    from core import security_auditor as sa
    calls = []
    orig = sa.subprocess.run

    def _spy(*a, **k):
        calls.append(a[0])
        raise FileNotFoundError("powershell")

    sa.subprocess.run = _spy
    try:
        for proto in ("TCP -Action Block ; Start-Process x ; echo", "TCP;x",
                      "'; x ;'", "ICMP", ""):
            calls.clear()
            assert sa._block_port_firewall(4455, proto) is False, proto
            assert not calls, proto
        for proto in ("TCP", "udp"):
            calls.clear()
            sa._blocked_ports.discard(4456)
            sa._block_port_firewall(4456, proto)
            assert calls, proto
        sa._blocked_ports.discard(4457)
        assert sa._block_port_firewall(70000, "TCP") is False
    finally:
        sa.subprocess.run = orig


# ── Round-16 (fresh independent review of the gen54 frozen candidate): the
# Round-15 write-action test only inspected tokens starting with "-", but git's
# LEGACY IMPLICIT SET form expresses the write purely in positionals
# (`git config <name> <value>`), so it reached the present-good predicate, which
# a read flag placed AFTER the operands satisfies — git parses with
# PARSE_OPT_STOP_AT_NON_OPTION and swallows that flag as the [value-pattern]
# operand. Confirmed live: rc=0, .git/config written, next ordinary git command
# ran the fsmonitor hook; with --global it writes $HOME/.gitconfig, outside every
# sandbox root. Fixed by ORDER: a read action must precede any operand. The same
# per-SUBCOMMAND-not-per-ACTION gap also let ref-writing actions through.
class TestGitConfigOrderAndRefWrites:
    @pytest.mark.parametrize("argv", [
        ["git", "config", "core.fsmonitor", "V", "--list"],
        ["git", "config", "core.fsmonitor", "V", "--get"],
        ["git", "config", "core.fsmonitor", "V", "-l"],
        ["git", "config", "core.fsmonitor", "V", "--get-all"],
        ["git", "config", "core.fsmonitor", "V", "--get-regexp"],
        ["git", "config", "--global", "core.fsmonitor", "V", "--list"],
        ["git", "config", "--system", "core.fsmonitor", "V", "--list"],
        ["git", "config", "-f", "cfgname", "sec.key", "V", "--list"],
        ["git", "config", "--local", "include.path", "f", "--list"],
        ["git", "config", "--type=path", "core.hooksPath", "hd", "--list"],
        ["git", "config", "core.pager", "touch"],
    ])
    def test_action_less_config_write_is_refused(self, argv):
        assert command_refusal(argv) is not None, argv

    @pytest.mark.parametrize("argv", [
        ["git", "config", "--get", "user.name"],
        ["git", "config", "--list"],
        ["git", "config", "-l"],
        ["git", "config", "--get-all", "remote.origin.url"],
        ["git", "config", "--get-regexp", "^user"],
        ["git", "config", "--local", "--list"],
        ["git", "config", "--global", "--list"],
        ["git", "config", "--show-origin", "--list"],
        ["git", "config", "--get", "--type=bool", "core.bare"],
        ["git", "config", "--list", "--name-only"],
        ["git", "config", "--get-urlmatch", "http", "hostonly"],
        ["git", "config", "--get", "-f", "cfgname", "sec.key"],
    ])
    def test_config_read_surface_survives(self, argv):
        assert command_refusal(argv) is None, argv

    @pytest.mark.parametrize("argv", [
        ["git", "branch", "-c", "a", "b"], ["git", "branch", "-C", "a", "b"],
        ["git", "branch", "--copy", "a", "b"], ["git", "branch", "-m", "a", "b"],
        ["git", "branch", "-d", "x"], ["git", "branch", "-D", "x"],
        ["git", "branch", "--create-reflog", "x"],
        ["git", "branch", "--edit-description"],
        ["git", "branch", "-u", "origin/x"], ["git", "branch", "--unset-upstream"],
        ["git", "branch", "newbranch"], ["git", "tag", "newtag"],
        ["git", "tag", "-d", "t"], ["git", "tag", "-a", "t"],
        ["git", "remote", "add", "n", "u"], ["git", "remote", "remove", "n"],
        ["git", "remote", "set-url", "n", "u"], ["git", "remote", "prune", "n"],
        ["git", "reflog", "expire", "--all"], ["git", "reflog", "delete", "x"],
        ["git", "symbolic-ref", "HEAD", "refs/heads/x"],
        ["git", "symbolic-ref", "-d", "HEAD"],
    ])
    def test_ref_writing_actions_are_refused(self, argv):
        assert command_refusal(argv) is not None, argv

    @pytest.mark.parametrize("argv", [
        ["git", "branch"], ["git", "branch", "-a"], ["git", "branch", "-v"],
        ["git", "branch", "--list"], ["git", "branch", "--list", "mas*"],
        ["git", "branch", "--contains", "HEAD"],
        ["git", "branch", "--merged", "HEAD"],
        ["git", "branch", "--points-at", "HEAD"],
        ["git", "branch", "--show-current"],
        ["git", "tag"], ["git", "tag", "-l"], ["git", "tag", "--list", "v1*"],
        ["git", "tag", "--points-at", "HEAD"],
        ["git", "remote"], ["git", "remote", "-v"],
        ["git", "remote", "show", "origin"], ["git", "remote", "get-url", "origin"],
        ["git", "reflog"], ["git", "reflog", "show", "HEAD"],
        ["git", "symbolic-ref", "HEAD"],
        # -c is a legitimate combined-diff read for log and must NOT be banned
        # globally just because `git branch -c` copies a branch.
        ["git", "log", "-c", "-n1"],
        ["git", "status"], ["git", "diff"], ["git", "show", "HEAD"],
        ["git", "rev-parse", "HEAD"], ["git", "describe"], ["git", "shortlog", "-n"],
        ["git", "ls-files"], ["git", "cat-file", "-p", "HEAD"],
        ["git", "for-each-ref"], ["git", "count-objects", "-v"], ["git", "version"],
    ])
    def test_read_only_git_surface_survives(self, argv):
        assert command_refusal(argv) is None, argv

    def test_git_query_inherits_the_ref_write_refusals(self):
        from tools.executor import ToolExecutor
        ex = ToolExecutor.__new__(ToolExecutor)
        for args in ("-c master copied_x", "--create-reflog newref",
                     "--edit-description", "newbranch", "-D x"):
            assert ex._tool_git_query(operation="branch", args=args).get("error"), args
        for op, args in (("status", ""), ("log", "-n 3"), ("diff", ""),
                         ("show", "HEAD"), ("branch", ""), ("branch", "-a"),
                         ("branch", "--list")):
            err = ex._tool_git_query(operation=op, args=args).get("error", "")
            assert not err, (op, args, err)


# ══════════════════ Round-17 — ref-write ABBREVIATIONS ══════════════════════
#
# Round-16 added a per-ACTION git ref-write grammar but matched it EXACT-or-`=`,
# the direction this module abandoned in Round-9. git's parse-options abbreviates
# every unambiguous long option for `branch`/`tag`/`symbolic-ref` too, and the
# bare-operand fallback masked most abbreviations by accident, so exactly the
# write actions taking NO required operand escaped. Confirmed live against git
# 2.53.0: `--edit-desc` wrote branch.<name>.description AND executed the ambient
# GIT_EDITOR, `--set-upstream-t=master` wrote branch.<name>.remote/.merge,
# `--unset-u` removed them, `symbolic-ref --del` deleted the ref — all reachable
# from git_query, which is READ_ONLY and HITL-EXEMPT (zero approvals).
class TestGitRefWriteAbbreviations:
    @pytest.mark.parametrize("arg", [
        "--edit-description", "--edit-descriptio", "--edit-descri", "--edit-desc",
        "--edit-de", "--edit-d",
        "--unset-upstream", "--unset-upstrea", "--unset-up", "--unset-u",
        "--create-reflog", "--create-reflo", "--create", "--creat", "--crea",
        "--force", "--forc", "--for", "--fo", "--f",
    ])
    def test_operandless_branch_write_abbreviations_are_refused(self, arg):
        assert command_refusal(["git", "branch", arg]) is not None, arg

    @pytest.mark.parametrize("arg", [
        "--set-upstream-to=master", "--set-upstream-t=master",
        "--set-upstream=master", "--set-upstrea=master", "--set-u=master",
        "--se=master",
    ])
    def test_branch_set_upstream_abbreviations_are_refused(self, arg):
        assert command_refusal(["git", "branch", arg]) is not None, arg

    @pytest.mark.parametrize("arg", ["--copy", "--cop", "--co", "--c",
                                     "--move", "--mov", "--mo", "--m"])
    def test_branch_copy_move_abbreviations_are_refused(self, arg):
        assert command_refusal(["git", "branch", arg, "x", "y"]) is not None, arg

    @pytest.mark.parametrize("arg", ["--delete", "--delet", "--dele", "--del",
                                     "--de", "--d"])
    def test_branch_delete_abbreviations_are_refused(self, arg):
        assert command_refusal(["git", "branch", arg, "x"]) is not None, arg

    @pytest.mark.parametrize("arg", ["-c", "-C", "-m", "-M", "-d", "-D", "-u", "-f"])
    def test_branch_short_writes_stay_refused(self, arg):
        assert command_refusal(["git", "branch", arg, "x", "y"]) is not None, arg

    @pytest.mark.parametrize("arg", ["--delete", "--dele", "--del", "--de", "--d"])
    def test_symbolic_ref_delete_abbreviations_are_refused(self, arg):
        assert command_refusal(
            ["git", "symbolic-ref", arg, "refs/heads/x"]) is not None, arg

    @pytest.mark.parametrize("arg", [
        "--delete", "--dele", "--annotate", "--annot", "--anno", "--ann",
        "--sign", "--sig", "--si", "--message=x", "--messag=x", "--mess=x",
        "--file", "--fil", "--fi", "--force", "--forc", "--create-reflog",
        "--creat",
    ])
    def test_tag_write_abbreviations_are_refused(self, arg):
        assert command_refusal(["git", "tag", arg, "t"]) is not None, arg

    @pytest.mark.parametrize("arg", ["-d", "-a", "-s", "-m", "-F", "-f"])
    def test_tag_short_writes_stay_refused(self, arg):
        assert command_refusal(["git", "tag", arg, "t"]) is not None, arg

    # The other half of parser-equivalence: an ABBREVIATED READ must stay a read.
    # This direction fails closed, so it was never a containment defect — but the
    # asymmetry is the shape this module exists to remove, and this test is what
    # keeps the write-side prefix matching from being "fixed" by over-refusing.
    @pytest.mark.parametrize("rest", [
        ["--con", "HEAD"], ["--conta", "HEAD"], ["--contains", "HEAD"],
        ["--no-contains", "HEAD"], ["--merge", "HEAD"], ["--me", "HEAD"],
        ["--merged", "HEAD"], ["--no-merged", "HEAD"], ["--point", "HEAD"],
        ["--po", "HEAD"], ["--points-at", "HEAD"], ["--sor=-committerdate"],
        ["--so=-committerdate"], ["--form=%(refname)"], ["--forma=%(refname)"],
        ["--format=%(refname)"], ["--show"], ["--sh"], ["--show-current"],
        ["--colum"], ["--column"], ["--list"], ["-a"], ["-v"], ["-vv"],
    ])
    def test_branch_read_abbreviations_survive(self, rest):
        assert command_refusal(["git", "branch", *rest]) is None, rest

    @pytest.mark.parametrize("rest", [
        ["--cont", "HEAD"], ["--merge", "HEAD"], ["--sort=v:refname"],
        ["--forma=%(refname)"], ["-l"], ["--list", "v1*"], ["--column"],
    ])
    def test_tag_read_abbreviations_survive(self, rest):
        assert command_refusal(["git", "tag", *rest]) is None, rest

    @pytest.mark.parametrize("rest", [["--shor", "HEAD"], ["--short", "HEAD"],
                                      ["-q", "HEAD"], ["HEAD"]])
    def test_symbolic_ref_read_survives(self, rest):
        assert command_refusal(["git", "symbolic-ref", *rest]) is None, rest

    # `-c` must NOT be banned globally: `git log -c` is a combined-diff READ.
    @pytest.mark.parametrize("argv", [
        ["git", "log", "-c", "-n1"], ["git", "log", "-m", "-n1"],
        ["git", "diff", "-M"], ["git", "diff", "-C"],
        ["git", "show", "-m", "HEAD"],
    ])
    def test_short_flags_of_other_subcommands_are_untouched(self, argv):
        assert command_refusal(argv) is None, argv

    @pytest.mark.parametrize("args", [
        "--edit-desc", "--edit-d", "--unset-u", "--set-upstream-t=master",
        "--creat", "--forc",
    ])
    def test_git_query_cannot_reach_write_abbreviations(self, args):
        ex = ToolExecutor.__new__(ToolExecutor)
        out = ex._tool_git_query(operation="branch", args=args)
        assert out.get("error"), (args, out)


# ═══════════ Round-17 — every path-shaped handler argument is gated ═════════
#
# `packet_tracer_open` consumed a caller path and handed it to Popen unexamined.
# It IS HITL-challenged (REVERSIBLE), but AUTHORIZATION IS NOT CONTAINMENT. The
# existing coverage test could not see it, because it only iterates the tools
# ALREADY in FILE_CAPABLE_TOOLS — an ABSENT control, invisible to a mutation
# campaign, which can only weaken a control that exists. This test inverts the
# direction: it scans every handler for a path-shaped parameter and requires the
# registry to know about it, so the NEXT such handler fails a test instead of
# shipping ungated.
_PATH_SHAPED = re.compile(
    r"(^|_)(path|paths|file|files|filepath|filename|archivo|archivos|ruta|"
    r"folder|dir|directory|carpeta)(_|$)", re.IGNORECASE)


class TestEveryPathShapedHandlerArgumentIsRegistered:
    def test_no_unregistered_path_shaped_handler_argument(self):
        import inspect
        unregistered: list[tuple[str, str]] = []
        for name in dir(ToolExecutor):
            if not name.startswith("_tool_"):
                continue
            tool = name[len("_tool_"):]
            try:
                sig = inspect.signature(getattr(ToolExecutor, name))
            except (TypeError, ValueError):  # pragma: no cover - defensive
                continue
            for param in sig.parameters:
                if param == "self" or not _PATH_SHAPED.search("_" + param + "_"):
                    continue
                if tool not in FILE_CAPABLE_TOOLS:
                    unregistered.append((tool, param))
        assert not unregistered, (
            "handler(s) take a path-shaped argument but are absent from "
            f"FILE_CAPABLE_TOOLS, so nothing checks their gate: {unregistered}")

    def test_the_scan_is_not_vacuous(self):
        """The regex must actually match the arguments it is meant to find —
        otherwise the test above passes by seeing nothing at all."""
        assert _PATH_SHAPED.search("_file_path_")
        assert _PATH_SHAPED.search("_folder_path_")
        assert _PATH_SHAPED.search("_filepath_")
        assert _PATH_SHAPED.search("_save_path_")
        assert not _PATH_SHAPED.search("_target_")
        found = 0
        import inspect
        for name in dir(ToolExecutor):
            if not name.startswith("_tool_"):
                continue
            try:
                sig = inspect.signature(getattr(ToolExecutor, name))
            except (TypeError, ValueError):  # pragma: no cover - defensive
                continue
            found += sum(1 for p in sig.parameters
                         if p != "self" and _PATH_SHAPED.search("_" + p + "_"))
        assert found >= 9, f"scan found only {found} path-shaped arguments"

    def test_packet_tracer_open_is_registered_and_gated(self):
        import inspect
        assert FILE_CAPABLE_TOOLS.get("packet_tracer_open") == (
            "file_path", FileIntent.READ)
        src = inspect.getsource(ToolExecutor._tool_packet_tracer_open)
        assert "_gate_path" in src

    def test_packet_tracer_open_refuses_a_path_outside_the_roots(self):
        ex = ToolExecutor.__new__(ToolExecutor)
        out = ex._tool_packet_tracer_open(file_path="/etc/hostname")
        assert out.get("error_code") == "PATH_NOT_ALLOWED", out
