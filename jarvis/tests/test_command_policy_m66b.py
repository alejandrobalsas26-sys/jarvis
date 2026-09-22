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
        # ── Round-3 (fresh independent review of the gen41 frozen candidate) ──
        "ffuf -input-cmd id -input-num 1 -u t",     # runs a host shell command
        "ffuf --input-cmd id -input-num 1 -u t",    # double-dash form
        "nikto -config evil.conf -h t",             # PLUGINDIR -> arbitrary Perl
        "nikto -conf evil.conf -h t",               # Getopt::Long abbreviation
        "nikto -co evil.conf -h t",                 # shortest unambiguous abbrev
        "tcpdump -i lo -w j -C 1 -z/bin/true",      # -z value ATTACHED to the flag
        "tcpdump -i lo -w j -nz /bin/true -C 1",    # -z CLUSTERED with -n
        # payload deliberately not a real absolute path: "=/bin/true" trips an
        # unrelated, pre-existing system-path guard in _validate_command before
        # the command-policy layer this test exercises is even reached.
        "tcpdump -i lo -w j -C 1 --postrotate-command=touch",  # long-form =value
        "msfconsole -p evil.rb",                    # `require`s arbitrary Ruby
        "msfconsole --plugin evil.rb",              # double-dash form
        "sqlmap -c evil.conf -u t",                 # config-file smuggles --eval
        "sqlmap --configFile evil.conf -u t",       # double-dash form
        # ── Round-4 (fresh independent review of the gen42 frozen candidate):
        # every Round-2/3 denier above used exact-token matching and missed the
        # SAME real binary's attached (`-Xvalue`) and/or `=`-attached
        # (`--long=value`, `-x=value`) forms — confirmed live against each
        # installed binary. Bare filenames (no "/") deliberately, to avoid the
        # unrelated pre-existing system-path guard noted above. ──
        "ffuf -input-cmd=id -input-num 1 -u t",     # ffuf's Go flag `=` form
        "ffuf --input-cmd=id -input-num 1 -u t",
        "nikto -config=evil.conf -h t",             # Getopt::Long `=` form
        "nikto -conf=evil.conf -h t",               # abbreviation + `=` form
        "msfconsole -pevil.rb",                     # attached, NO separator
        "msfconsole --plugin=evil.rb",              # `=` form
        "msfconsole -xevil.rc",                     # -x attached, NO separator
        "sqlmap -cevil.ini -u t",                   # attached, NO separator
        # ── Round-5 (fresh independent review of the gen43 frozen candidate):
        # two more "purpose-built"/incompletely-checked lab tools had a real
        # generic-exec escape via a config/module-loading option — a NEW class
        # of gap (not just another attached/= spelling), confirmed live. ──
        "john --config=evil.conf --external=Evil --stdout",  # trusted-input
        "john --con=evil.conf --ext=Evil --stdout",           # abbreviated
        "msfconsole -q -n --no-defer-module-loads -m evildir",  # module_eval
        "msfconsole --module-path=evildir --no-defer-module-loads",
        "msfconsole -mevildir",                      # attached, NO separator
        # ── Round-6 (fresh independent review of the gen44 frozen candidate):
        # a THIRD, distinct msfconsole config-loading flag, unaudited by
        # rounds 2-5. See test_alternate_execution_doors_m66b.py for the
        # separate, more severe write_file+.git/config finding this round. ──
        "msfconsole -c evil.yml",                    # Framework config load
        "msfconsole -cevil.yml",                      # attached, NO separator
        "msfconsole --config=evil.yml",
        # ── Round-7 (fresh independent review of the gen45 frozen candidate):
        # hashcat v7's Assimilation Bridge (-m 72000/73000) `import`s a
        # caller-overridable Python file via --bridge-parameter1..4 — a
        # well-known GPU cracker's obscure newest feature is an embedded
        # generic-code-execution primitive, none of rounds 1-6 examined it. ──
        "hashcat -m 73000 --bridge-parameter1 evil.py h w",
        "hashcat -m 73000 --bridge-parameter1=evil.py h w",
        "hashcat -m 72000 --bridge-parameter4 evil.py h w",
        # ── Round-8 (fresh independent review of the gen46 frozen candidate):
        # Round-5's reasoning that msfconsole -M/--migration-path is merely
        # "different from -m and legitimate" was WRONG — it independently
        # verified -m is dangerous but never checked -M itself. A caller-
        # supplied directory is appended to migrations_paths and every
        # pending .rb file under it is LOADED (running its top-level code
        # unconditionally, standard ActiveRecord/Rails behavior) at ordinary
        # console startup — confirmed live via a real throwaway Postgres +
        # planted migration file. ──
        "msfconsole -M evildir",
        "msfconsole --migration-path evildir",
        "msfconsole -Mevildir",                       # attached, NO separator
        "msfconsole --migration-path=evildir",
    ])
    def test_lab_generic_host_exec_is_blocked(self, cmd):
        assert _blocked(cmd, _LAB_COMMAND_ALLOWLIST), cmd

    @pytest.mark.parametrize("cmd", [
        "masscan 1.2.3.4", "nikto -h 1.2.3.4", "hydra -l a -p b t",
        "gobuster dir -u t", "ffuf -u t", "tcpdump -i lo -c 5",
        "tshark -i lo -c 5", "sqlmap -u http_target --batch",
        "hashcat -m 0 h w", "john hashfile",
        # Round-3: msfvenom's -p is --payload (mandatory, core usage) and its -o
        # is --out (mandatory for every real invocation) — NOT msfconsole's
        # plugin loader. Must NOT be broken by the msfconsole -p/--plugin fix.
        # (payload name deliberately slash-free: a real `windows/meterpreter/…`
        # value trips an unrelated, pre-existing system-path guard in
        # _validate_command that has nothing to do with the lab command policy
        # this test exercises.)
        "msfvenom -p mypayload LHOST=1.2.3.4 LPORT=4444 -f exe -o out.exe",
        # Round-4: sqlmap's -C (uppercase, column enumeration — core legitimate
        # functionality) is a DIFFERENT flag from -c (config file) despite
        # colliding once lowercased; must not be broken by the -c fix above.
        "sqlmap -u t -C user,pass --batch",
        # Round-5: john's --wordlist mode must not be broken by the new
        # --config/--external denier (bare "john hashfile" above already
        # covers the plain crack mode).
        "john --wordlist=rockyou.txt hashfile",
        # Round-7: hashcat mode 72000/73000 WITHOUT a caller-supplied bridge
        # parameter is legitimate (hashcat's own bundled bridge); only the
        # plugin-override flags are refused.
        "hashcat -m 73000 h w",
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
        # Round-4: curl's own getopt parser accepts -K's value ATTACHED with no
        # separator — an exact `a == "-K"` token check missed this.
        assert command_refusal(["curl", "-Kevilrc"]) is not None
        assert command_refusal(["curl", "example.com"]) is None
        # -k (lowercase, --insecure) is a DIFFERENT, harmless flag and must
        # never be caught by the -K denier.
        assert command_refusal(["curl", "-k", "example.com"]) is None

    def test_unknown_binary_defaults_to_deny(self):
        # A binary with no classification is treated as REMOVED (fail closed).
        assert command_policy.classify("totally_unknown_binary_xyz") \
            is CommandCapability.REMOVED
        assert command_refusal(["totally_unknown_binary_xyz", "x"]) is not None
