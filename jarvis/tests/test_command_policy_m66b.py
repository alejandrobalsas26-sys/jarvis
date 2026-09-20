"""tests/test_command_policy_m66b.py — V69 M66B Round-1 (§7/§8/§11/§12).

The command-SEMANTIC policy that closes the LOLBin door: an allowlist of
executable NAMES is not an execution policy. Round 1 broke the earlier
interpreter-only block via git -c / nmap --script / wget --use-askpass / find
-exec / scp -oProxyCommand / pip install / ssh. These prove the policy now
refuses every command-spawning vector while keeping legitimate diagnostics.

Every fixture is a harmless local synthetic (no host/network probe): the point is
whether _validate_command / command_refusal ACCEPTS the shape, not what it does.
"""
from __future__ import annotations

import pytest

from core import command_policy
from core.command_policy import CommandCapability, HOST_COMMAND_POLICY, command_refusal
from tools.executor import COMMAND_ALLOWLIST, _LAB_COMMAND_ALLOWLIST, _validate_command


def _blocked(cmd, extra=frozenset()):
    ok, _msg, _ = _validate_command(cmd, extra)
    return not ok


def _allowed(cmd, extra=frozenset()):
    ok, _msg, _ = _validate_command(cmd, extra)
    return ok


# ── GIT — positive subcommand grammar; config/helper/exec vectors refused ─────
class TestGit:
    @pytest.mark.parametrize("cmd", [
        "git status", "git log --oneline", "git diff", "git show HEAD",
        "git rev-parse HEAD", "git branch", "git config --get user.name",
    ])
    def test_read_only_git_still_works(self, cmd):
        assert _allowed(cmd), cmd

    @pytest.mark.parametrize("cmd", [
        "git -c core.fsmonitor=touch status",
        "git -ccore.pager=touch log",
        "git -C /tmp status",
        "git --exec-path=/tmp status",
        "git config core.pager touch",          # config WRITE
        "git difftool --extcmd=touch",
        "git submodule foreach touch",
        "git filter-branch --tree-filter touch",
        "git commit -c HEAD",                    # -c reuse-message: not read-only
    ])
    def test_git_execution_vectors_blocked(self, cmd):
        assert _blocked(cmd), cmd


# ── NMAP — diagnostic works; NSE scripting refused ────────────────────────────
class TestNmap:
    def test_diagnostic_nmap_works(self):
        assert _allowed("nmap -sV --top-ports 100 1.2.3.4")

    @pytest.mark.parametrize("cmd", [
        "nmap --script=http-title 1.2.3.4",
        "nmap --script http-title 1.2.3.4",
        "nmap --script-args x=1 1.2.3.4",
        "nmap --datadir /tmp 1.2.3.4",
    ])
    def test_nmap_nse_blocked(self, cmd):
        assert _blocked(cmd), cmd


# ── WGET — helper/askpass refused ─────────────────────────────────────────────
class TestWget:
    def test_plain_wget_host_works(self):
        assert _allowed("wget example.com")

    @pytest.mark.parametrize("cmd", [
        "wget --use-askpass=touch example.com",
        "wget -e use_askpass=touch example.com",
        "wget --config=evil example.com",
    ])
    def test_wget_helpers_blocked(self, cmd):
        assert _blocked(cmd), cmd


# ── FIND — read/query works; -exec/-delete refused ────────────────────────────
class TestFind:
    def test_find_query_works(self):
        assert _allowed("find . -name x.py")

    @pytest.mark.parametrize("cmd", [
        "find . -exec touch X ;", "find . -execdir touch X ;",
        "find . -ok touch X ;", "find . -okdir touch X ;",
        "find . -delete", "find . -fprintf out fmt",
    ])
    def test_find_exec_blocked(self, cmd):
        assert _blocked(cmd), cmd


# ── REMOVED binaries — not in the generic host gateway at all ─────────────────
class TestRemoved:
    @pytest.mark.parametrize("binary", [
        "ssh", "scp", "pip", "pip3", "gcc", "g++", "openssl",
        "perl", "ruby", "php", "awk", "gawk", "sed", "tar", "rsync",
        "env", "xargs", "bash", "sh", "zsh", "pwsh", "powershell",
    ])
    def test_removed_binaries_blocked(self, binary):
        assert _blocked(f"{binary} whatever")
        assert binary not in COMMAND_ALLOWLIST

    @pytest.mark.parametrize("cmd", [
        "ssh -oProxyCommand=touch h", "scp -oProxyCommand=touch a b",
        "pip install requests", "pip3 install -e .",
        "gcc -wrapper /bin/sh x.c", "awk BEGIN{system(1)}",
        "env touch", "xargs touch", "tar --to-command=touch",
    ])
    def test_removed_binary_exec_vectors_blocked(self, cmd):
        assert _blocked(cmd), cmd


# ── INTERPRETERS — informational flags only ───────────────────────────────────
class TestInterpreters:
    @pytest.mark.parametrize("cmd", [
        "python attacker.py", "python3 attacker.py", "python -m evil",
        "node a.js", "npm run evil", "make", "make target",
    ])
    def test_interpreter_code_blocked(self, cmd):
        ok, msg, _ = _validate_command(cmd)
        assert not ok
        assert "code_execute" in msg or "not permitted" in msg

    @pytest.mark.parametrize("cmd", [
        "python --version", "python3 --version", "node --version",
    ])
    def test_interpreter_info_flags_allowed(self, cmd):
        assert _allowed(cmd), cmd


# ── GLOBAL injection fragments — refused on any binary ────────────────────────
class TestGlobalInjection:
    @pytest.mark.parametrize("frag", [
        "-oProxyCommand=touch", "-oLocalCommand=touch", "--to-command=touch",
        "--checkpoint-action=exec=touch", "--use-compress-program=touch",
    ])
    def test_injection_fragment_refused(self, frag):
        # Even on a SAFE binary, a command-spawning fragment is refused.
        assert command_refusal(["ping", frag]) is not None


# ── Lab offensive tools: operator-gated, out of base scope (documented) ───────
class TestTrustedLab:
    def test_lab_only_binary_allowed_under_lab(self):
        assert _allowed("masscan 1.2.3.4", _LAB_COMMAND_ALLOWLIST)

    def test_base_binary_still_governed_under_lab(self):
        # A base binary stays governed even with the lab allowlist active.
        assert _blocked("git -c core.pager=touch status", _LAB_COMMAND_ALLOWLIST)
        assert _blocked("python attacker.py", _LAB_COMMAND_ALLOWLIST)

    # ── Round-2 F1: lab tools are GOVERNED, not blanket-exempt ────────────────
    @pytest.mark.parametrize("cmd", [
        "tcpdump -i lo -w j -G 1 -z touch",     # -z = host command per rotation
        "tshark -X lua_script:evil.lua -r j",   # -X lua = host Lua
        "tshark -Xlua_script:evil.lua",         # clustered form
        "sqlmap --eval print -u t",             # --eval = host Python
        "sqlmap --alert touch -u t",            # --alert = host command
        "msfconsole -x irb",                    # -x irb = host Ruby shell
        "msfconsole -r evil.rc",                # -r = arbitrary resource script
    ])
    def test_lab_generic_host_exec_is_blocked(self, cmd):
        assert _blocked(cmd, _LAB_COMMAND_ALLOWLIST), cmd

    @pytest.mark.parametrize("cmd", [
        "masscan 1.2.3.4", "nikto -h 1.2.3.4", "hydra -l a -p b t",
        "gobuster dir -u t", "ffuf -u t", "tcpdump -i lo -c 5",
        "tshark -i lo -c 5", "sqlmap -u http_target --batch",
        "hashcat -m 0 h w", "john hashfile",
    ])
    def test_lab_purpose_built_still_works(self, cmd):
        assert _allowed(cmd, _LAB_COMMAND_ALLOWLIST), cmd

    def test_unknown_lab_binary_fails_closed(self):
        from core.command_policy import command_refusal
        assert command_refusal(["totally_unknown_lab_xyz", "x"], lab=True) is not None


# ── Structural coverage (§12): the policy classifies every base binary ────────
class TestPolicyCoverage:
    def test_every_base_binary_is_classified_non_removed(self):
        for binary in COMMAND_ALLOWLIST:
            cap = HOST_COMMAND_POLICY.get(binary)
            assert cap is not None, f"{binary} in allowlist but unclassified"
            assert cap is not CommandCapability.REMOVED, (
                f"{binary} is REMOVED yet still in COMMAND_ALLOWLIST")

    def test_removed_class_binaries_are_absent_from_allowlist(self):
        removed = {b for b, c in HOST_COMMAND_POLICY.items()
                   if c is CommandCapability.REMOVED}
        assert removed.isdisjoint(COMMAND_ALLOWLIST)

    # ── Unit-level: each policy RULE tested in isolation via command_refusal ──
    # _validate_command layers a metacharacter block and a name allowlist on top;
    # those can MASK a weakened policy rule (e.g. `find . -exec … ;` is refused by
    # the `;` metacharacter regardless of the find rule). Testing command_refusal
    # directly makes every individual rule load-bearing.
    def test_find_rule_is_load_bearing(self):
        assert command_refusal(["find", ".", "-exec", "touch", "X"]) is not None
        assert command_refusal(["find", ".", "-delete"]) is not None
        assert command_refusal(["find", ".", "-name", "x.py"]) is None

    def test_removed_binary_rule_is_load_bearing(self):
        # A REMOVED binary must be refused by the POLICY even if it reached it.
        for b in ("ssh", "scp", "pip", "gcc", "openssl", "awk", "env", "xargs"):
            assert command_refusal([b, "whatever"]) is not None, b

    def test_git_global_option_rules_are_load_bearing(self):
        assert command_refusal(["git", "-C", "/tmp", "status"]) is not None
        assert command_refusal(["git", "--exec-path=/tmp", "status"]) is not None
        assert command_refusal(["git", "status"]) is None

    def test_curl_config_rule_is_load_bearing(self):
        assert command_refusal(["curl", "--config", "evilrc"]) is not None
        assert command_refusal(["curl", "example.com"]) is None

    def test_unknown_binary_defaults_to_deny(self):
        # A binary with no classification is treated as REMOVED (fail closed).
        assert command_policy.classify("totally_unknown_binary_xyz") \
            is CommandCapability.REMOVED
        assert command_refusal(["totally_unknown_binary_xyz", "x"]) is not None
