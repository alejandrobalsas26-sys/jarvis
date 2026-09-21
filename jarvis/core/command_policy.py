"""core/command_policy.py — V69 M66B Round-1: command-SEMANTIC host policy.

WHY THIS EXISTS
---------------
An allowlist of executable NAMES is not an execution policy. Round 1 falsified
the "second door is closed" claim: `run_shell_command` / `RedTeamShellExecutor`
ran caller-controlled arbitrary host code through allowlisted binaries whose
ARGUMENTS spawn programs:

    scp -oProxyCommand=<cmd>      git -c core.fsmonitor=<cmd> status
    pip install <pkg>            nmap --script=<file>.nse
    wget --use-askpass=<cmd>     find . -exec <cmd> ;

`git` runs helpers; `find` runs `-exec`; `nmap` runs NSE scripts; `wget` invokes
askpass helpers; `pip` runs setup code; `ssh`/`scp` run ProxyCommand. HITL and
AuthorizedSecurityScope decide AUTHORITY, not containment — so this gateway must
PROVE that a permitted command cannot become caller-controlled arbitrary host
code through its arguments, config, plugins, helpers, hooks or subcommands.

THE MODEL
---------
Every host-gateway binary carries a capability classification:

  * SAFE_FIXED_DIAGNOSTIC   — cannot execute code/commands regardless of args
    (ping, whois, ps, cat, grep, echo, …). Any argv is accepted (still subject to
    the executor's metacharacter and system-path checks).
  * SAFE_WITH_ARGUMENT_POLICY — safe ONLY when a structural argument policy holds
    (git, nmap, wget, find, curl). The policy is a POSITIVE grammar where
    practical (git subcommands) and a targeted structural denier for the specific
    command-spawning options otherwise.
  * EXECUTION_CAPABLE       — an interpreter / build tool / installer whose whole
    job is to run code (python, node, npm, make, …). Only purely informational
    flags (`--version`, `--help`) are permitted; anything that runs a script, a
    module or inline code is refused and routed to code_execute.
  * REMOVED                 — not permitted in the host gateway at all
    (pip, pip3, gcc, ssh, scp, openssl). Kept here so re-adding one to the
    allowlist without a policy is caught by the coverage test.

A binary the allowlist contains but this policy does not classify is treated as
REMOVED (deny) — a new binary cannot enter the gateway un-vetted.

Arbitrary code ALWAYS has exactly one door: code_execute → the SANDBOX_REQUIRED
ContainmentBroker. This module makes the host gateway structurally incapable of
being a second one.
"""
from __future__ import annotations

from enum import Enum
from pathlib import PurePosixPath, PureWindowsPath


class CommandCapability(str, Enum):
    SAFE_FIXED_DIAGNOSTIC = "safe_fixed_diagnostic"
    SAFE_WITH_ARGUMENT_POLICY = "safe_with_argument_policy"
    EXECUTION_CAPABLE = "execution_capable"
    REMOVED = "removed"
    #: V69 M66B Round-2: a purpose-built, operator-authorized offensive/lab tool
    #: (trusted-lab only). It legitimately needs host network/packet/target access
    #: — that is its INTENDED function, not a defect — but caller arguments must
    #: not turn it into GENERIC host arbitrary-code execution. Allowed as-is only
    #: when it has no reachable generic-host-exec escape; otherwise it is
    #: SAFE_WITH_ARGUMENT_POLICY with a lab denier.
    PURPOSE_BUILT_AUTHORIZED = "purpose_built_authorized"


#: Purely informational flags an EXECUTION_CAPABLE binary may carry (no code runs).
_INFO_FLAGS: frozenset[str] = frozenset({
    "--version", "-v", "-V", "--help", "-h", "version", "--info",
})

#: Cross-tool command-injection option fragments. If ANY argument contains one of
#: these (case-insensitively), the command is refused whatever the binary — these
#: name a helper/command an option would spawn. Defense in depth: they apply even
#: to SAFE binaries and to any binary re-added to the allowlist.
_GLOBAL_INJECTION_FRAGMENTS: tuple[str, ...] = (
    "proxycommand",            # ssh/scp -oProxyCommand=
    "localcommand",            # ssh/scp -oLocalCommand= / PermitLocalCommand
    "--use-askpass",           # wget/git askpass helper
    "--to-command",            # tar --to-command=
    "--checkpoint-action",     # tar --checkpoint-action=exec=
    "--use-compress-program",  # tar -I / --use-compress-program=
    "-execcmd",                # rsync-style helpers
    "ext::",                   # git remote ext:: transport
    "--wrapper",               # gcc -wrapper
)


# ── The classification for every binary the host gateway may see ─────────────
HOST_COMMAND_POLICY: dict[str, CommandCapability] = {
    # SAFE_FIXED_DIAGNOSTIC — no code/command execution regardless of arguments.
    "ping": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "whois": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "traceroute": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "tracert": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "netstat": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "ipconfig": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "ifconfig": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "arp": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "ps": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "top": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "htop": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "tasklist": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "df": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "du": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "free": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "uname": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "hostname": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "whoami": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "id": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "ls": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "dir": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "cat": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "type": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "more": CommandCapability.SAFE_FIXED_DIAGNOSTIC,   # no tty → no `!cmd` escape
    "head": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "tail": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "wc": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "echo": CommandCapability.SAFE_FIXED_DIAGNOSTIC,
    "grep": CommandCapability.SAFE_FIXED_DIAGNOSTIC,   # no exec option
    "findstr": CommandCapability.SAFE_FIXED_DIAGNOSTIC,

    # SAFE_WITH_ARGUMENT_POLICY — structurally constrained.
    "git": CommandCapability.SAFE_WITH_ARGUMENT_POLICY,
    "nmap": CommandCapability.SAFE_WITH_ARGUMENT_POLICY,
    "wget": CommandCapability.SAFE_WITH_ARGUMENT_POLICY,
    "curl": CommandCapability.SAFE_WITH_ARGUMENT_POLICY,
    "find": CommandCapability.SAFE_WITH_ARGUMENT_POLICY,

    # EXECUTION_CAPABLE — interpreters / build tools; informational flags only.
    "python": CommandCapability.EXECUTION_CAPABLE,
    "python2": CommandCapability.EXECUTION_CAPABLE,
    "python3": CommandCapability.EXECUTION_CAPABLE,
    "node": CommandCapability.EXECUTION_CAPABLE,
    "nodejs": CommandCapability.EXECUTION_CAPABLE,
    "npm": CommandCapability.EXECUTION_CAPABLE,
    "npx": CommandCapability.EXECUTION_CAPABLE,
    "make": CommandCapability.EXECUTION_CAPABLE,

    # REMOVED — not permitted in the generic host gateway.
    "pip": CommandCapability.REMOVED,
    "pip3": CommandCapability.REMOVED,
    "gcc": CommandCapability.REMOVED,
    "g++": CommandCapability.REMOVED,
    "ssh": CommandCapability.REMOVED,
    "scp": CommandCapability.REMOVED,
    "openssl": CommandCapability.REMOVED,
    "perl": CommandCapability.REMOVED,
    "ruby": CommandCapability.REMOVED,
    "php": CommandCapability.REMOVED,
    "awk": CommandCapability.REMOVED,   # awk 'BEGIN{system(...)}' — code engine
    "gawk": CommandCapability.REMOVED,
    "sed": CommandCapability.REMOVED,   # sed 'e cmd' / w file — exec/write engine
    "tar": CommandCapability.REMOVED,   # --to-command / -I
    "rsync": CommandCapability.REMOVED,  # -e / rsync-path
    "env": CommandCapability.REMOVED,   # env <cmd> runs a program
    "xargs": CommandCapability.REMOVED,  # xargs <cmd> runs a program
    "bash": CommandCapability.REMOVED,
    "sh": CommandCapability.REMOVED,
    "zsh": CommandCapability.REMOVED,
    "pwsh": CommandCapability.REMOVED,
    "powershell": CommandCapability.REMOVED,
}


def _basename(token: str) -> str:
    """Executable basename, POSIX and Windows, lowercased, without .exe."""
    name = PurePosixPath(token).name
    if "\\" in token or (len(token) >= 2 and token[1] == ":"):
        name = PureWindowsPath(token).name
    return name.lower().removesuffix(".exe")


# ── Per-binary structural policies for SAFE_WITH_ARGUMENT_POLICY ──────────────
#: git: a POSITIVE subcommand grammar. Only read-only porcelain/plumbing, and no
#: global config/exec option before the subcommand. `git -c k=v`, aliases,
#: difftool/mergetool --extcmd, submodule foreach, filter-branch and `git shell`
#: are all command-execution vectors and are refused by construction.
_GIT_ALLOWED_SUBCOMMANDS: frozenset[str] = frozenset({
    "status", "log", "diff", "show", "branch", "remote", "rev-parse", "describe",
    "blame", "shortlog", "ls-files", "ls-tree", "cat-file", "symbolic-ref",
    "for-each-ref", "reflog", "whatchanged", "count-objects", "version", "tag",
    "config",  # only with --get/--list; enforced below
})
_GIT_DENIED_GLOBAL_PREFIXES: tuple[str, ...] = (
    "-c", "-C", "--exec-path", "--config-env", "--namespace", "--work-tree",
    "--git-dir", "--upload-pack", "--receive-pack",
)


def _git_reason(args: list[str]) -> str | None:
    subcommand = None
    i = 0
    while i < len(args):
        a = args[i]
        low = a.lower()
        if a.startswith("-"):
            for pref in _GIT_DENIED_GLOBAL_PREFIXES:
                if low == pref or low.startswith(pref + "=") or (
                        pref in ("-c", "-C") and low.startswith(pref) and len(low) > 2):
                    return (f"git global option {a!r} can execute a helper/command; "
                            "refused")
            i += 1
            continue
        subcommand = low
        rest = [x.lower() for x in args[i + 1:]]
        break
    if subcommand is None:
        return None  # bare `git` prints usage; no execution
    if subcommand not in _GIT_ALLOWED_SUBCOMMANDS:
        return (f"git subcommand {subcommand!r} is not in the read-only host "
                "gateway allowlist; refused")
    # Even allowed subcommands must not carry a command-spawning option.
    banned_sub_opts = ("--extcmd", "--tool", "-x", "--exec", "--open-files-in-pager",
                       "-o", "--output", "foreach", "--upload-pack", "--receive-pack")
    for r in rest:
        if any(r == b or r.startswith(b + "=") for b in banned_sub_opts):
            return f"git {subcommand} option carrying a command is refused"
    if subcommand == "config":
        if not any(r in ("--get", "--list", "-l", "--get-all", "--get-regexp")
                   for r in rest):
            return "git config may only READ (--get/--list) via the host gateway"
    return None


def _nmap_reason(args: list[str]) -> str | None:
    for a in args:
        low = a.lower()
        if low.startswith("--script") or low == "--datadir" or low.startswith("--datadir="):
            return "nmap NSE scripting (--script/--datadir) executes code; refused"
    return None


def _wget_reason(args: list[str]) -> str | None:
    for a in args:
        low = a.lower()
        if low == "--use-askpass" or low.startswith("--use-askpass="):
            return "wget --use-askpass runs a helper command; refused"
        if low in ("-e", "--execute") or low.startswith("--execute="):
            return "wget -e/--execute sets .wgetrc directives (askpass); refused"
        if low.startswith("--config"):
            return "wget --config can set an askpass helper; refused"
    return None


def _curl_reason(args: list[str]) -> str | None:
    # curl -K/--config reads a config file that can chain requests and redirect
    # output over files. (-K is the config flag; lowercase -k is --insecure and
    # is harmless, so it is deliberately NOT matched — case-SENSITIVE on purpose.)
    #
    # Round-4 (fresh independent review, gen42 frozen candidate): curl's getopt
    # parser accepts -K's value ATTACHED with no separator (`-Kfile`), same as
    # its other short options — confirmed live against the installed binary.
    # An exact `a == "-K"` token check missed this; `startswith` (still
    # case-sensitive, so "-k..." never matches) catches bare, attached and any
    # future `-K=file` spelling in one check.
    for a in args:
        low = a.lower()
        if a.startswith("-K") or low == "--config" or low.startswith("--config="):
            return "curl -K/--config can drive file-overwriting requests; refused"
    return None


def _find_reason(args: list[str]) -> str | None:
    banned = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprintf",
              "-fprint", "-fprint0", "-fls"}
    for a in args:
        if a.lower() in banned:
            return f"find {a} executes commands or mutates the host; refused"
    return None


_ARG_POLICIES = {
    "git": _git_reason,
    "nmap": _nmap_reason,
    "wget": _wget_reason,
    "curl": _curl_reason,
    "find": _find_reason,
}


# ── Trusted-lab tools (JARVIS_TRUSTED_LAB + FULL_NATO) ────────────────────────
# Round-2 F1: a blanket exemption made the lab allowlist a SECOND generic-host-exec
# door — `tcpdump -z <cmd>` runs a host program per rotation, `tshark -X
# lua_script:` loads host Lua, `sqlmap --eval` evaluates host Python, `msfconsole
# -x irb` opens a host Ruby shell. These are NOT their offensive purpose; they are
# accidental generic escapes. Lab tools are now GOVERNED by this policy too:
# purpose-built ones run as-is (their target/network access IS the point), and the
# ones with a generic-host-exec option carry a lab argument denier.
#
# Round-3 (fresh independent review, gen 41 frozen candidate): "purpose-built has
# no generic-exec escape" was asserted, not verified, for two of the eleven —
# `ffuf -input-cmd`/`-input-shell` runs a host shell command to generate fuzz
# input (its own documented feature), and `nikto -config` can point PLUGINDIR at
# an attacker-controlled directory, so nikto (Perl) `require`s an arbitrary
# `*.plugin` file on startup. Both moved out of the no-arg-policy set.
#
# Round-5 (fresh independent review, gen43 frozen candidate): `john --config`
# can point at an attacker-authored config file carrying a `[List.External:MODE]`
# section, which John's own documentation states is TRUSTED, executable input
# (`--external=MODE` selects it) — confirmed live. Also moved out.
#
# Round-7 (fresh independent review, gen45 frozen candidate): hashcat v7's
# "Assimilation Bridge" (`-m 72000`/`73000`) loads a caller-overridable Python
# plugin via `--bridge-parameter1..4` and `import`s it — all of its top-level
# code runs before any cracking attempt, confirmed live with a canary file
# (ran even though the GPU/OpenCL backend itself failed on this host). A
# well-known GPU hash-cracker's rarely-examined newest feature is an embedded
# generic-code-execution primitive; also moved out.
_LAB_PURPOSE_BUILT: frozenset[str] = frozenset({
    "masscan", "hydra", "gobuster", "dirb", "sliver",
    "responder", "crackmapexec",
})
_LAB_ARGUMENT_POLICY: frozenset[str] = frozenset({
    "tcpdump", "tshark", "sqlmap", "msfconsole", "msfvenom", "ffuf", "nikto",
    "john", "hashcat",
})
LAB_COMMAND_POLICY: dict[str, CommandCapability] = {
    b: CommandCapability.PURPOSE_BUILT_AUTHORIZED for b in _LAB_PURPOSE_BUILT}
LAB_COMMAND_POLICY.update(
    {b: CommandCapability.SAFE_WITH_ARGUMENT_POLICY for b in _LAB_ARGUMENT_POLICY})


def _tcpdump_reason(args: list[str]) -> str | None:
    # Round-3: the prior exact-token check (`a.lower() in ("-z", ...)`) missed
    # tcpdump's own getopt forms for the SAME flag — attached (`-z/path/x`) and
    # clustered with a preceding boolean short flag (`-nz /path/x`). Verified
    # against the real binary: both forms reach tcpdump's "-z cannot be used
    # without -w and (-C or -G)" semantic check, i.e. both are parsed as -z with
    # the trailing/following token as ITS argument, not two unrelated flags. A
    # single-dash cluster containing 'z' anywhere is refused; long-form is
    # refused by prefix so `--postrotate-command=<x>` is covered too.
    for a in args:
        low = a.lower()
        if low.startswith("--postrotate-command"):
            return "tcpdump --postrotate-command runs a host command; refused"
        if low.startswith("-") and not low.startswith("--") and "z" in low[1:]:
            return ("tcpdump -z (bare, attached or clustered) runs a host command "
                     "on each rotation; refused")
    return None


def _tshark_reason(args: list[str]) -> str | None:
    for a in args:
        low = a.lower()
        if low == "-x" or low.startswith("-x") or "lua_script" in low:
            return "tshark -X extension / Lua scripting executes host code; refused"
    return None


def _sqlmap_reason(args: list[str]) -> str | None:
    # Round-3: sqlmap's own `-c/--configFile` loads an INI file whose `evalCode`/
    # `alert` keys map to the SAME internal options as `--eval`/`--alert`
    # (verified against the installed sqlmap's optiondict). Upstream sqlmap
    # independently refuses those two keys unless SQLMAP_UNSAFE_EVAL / _ALERT is
    # set in the environment — but RedTeamShellExecutor inherits the host
    # process's environment, so that is an external tool's own gate, not this
    # policy's. Refuse the config-file vector directly rather than rely on it.
    #
    # Round-4: sqlmap's own CLI parser accepts `-c`'s value ATTACHED with no
    # separator (`-c<file>`), same as its other short options — confirmed live
    # against the installed binary (it opened the exact attacker-given path).
    # `a.startswith("-c")` (case-SENSITIVE) catches bare, attached and any
    # future `-c=file` spelling in one check. Case-sensitive on purpose: sqlmap
    # has a DIFFERENT, legitimate `-C COL` (uppercase — column enumeration,
    # core functionality) that a case-INSENSITIVE check would wrongly refuse;
    # `--configFile` stays a case-insensitive long-option comparison since
    # there is no colliding differently-cased long option.
    for a in args:
        low = a.lower()
        if (low in ("--eval", "--alert") or low.startswith("--eval=")
                or low.startswith("--alert=") or a.startswith("-c")
                or low.startswith("--configfile")):
            return ("sqlmap --eval/--alert (directly or via -c/--configFile) "
                     "evaluates/runs host code; refused")
    return None


def _msfconsole_reason(args: list[str]) -> str | None:
    # Round-4: msfconsole's Ruby OptionParser accepts every one of these
    # options' value ATTACHED with no separator (`-p<path>`) as well as the
    # GNU `--long=value` form — confirmed live against the installed binary for
    # -p/--plugin (a bare `require()` executed before any plugin-validity
    # check). `startswith` on both the short and long spelling catches bare,
    # attached and `=`-attached forms in one check; no other msfconsole flag
    # begins with "-p", "-x" or "-r" (checked against the installed --help).
    #
    # Round-5: `-m/--module-path DIRECTORY` ("Load an additional module path")
    # combined with `--[no-]defer-module-loads` forces eager `module_eval` of
    # every .rb file under the given directory at startup — confirmed live
    # (a synthetic module's top-level code ran with no -x/-r/-p at all).
    # Refusing -m/--module-path alone closes it: without an attacker-added
    # module path, --no-defer-module-loads has nothing extra to eagerly load.
    # Case-SENSITIVE on "-m": msfconsole also has a DIFFERENT, legitimate
    # `-M/--migration-path` (uppercase — DB migrations) that a case-insensitive
    # check would wrongly refuse; the long forms don't collide even lowercased.
    for a in args:
        low = a.lower()
        if (low.startswith("-x") or low.startswith("-r")
                or low.startswith("--resource")):
            return ("msfconsole -x/-r runs arbitrary console/resource commands "
                    "(e.g. irb = host Ruby shell); refused")
        if low.startswith("-p") or low.startswith("--plugin"):
            return ("msfconsole -p/--plugin `require`s an arbitrary Ruby file on "
                     "startup, executing its top-level code before any plugin "
                     "validity check; refused")
        if a.startswith("-m") or low.startswith("--module-path"):
            return ("msfconsole -m/--module-path eagerly module_eval()s "
                     "arbitrary Ruby under the given directory with "
                     "--no-defer-module-loads; refused")
        # Round-6: a THIRD, distinct config-loading flag — msfconsole's own
        # Framework `-c FILE` ("Load the specified configuration file", under
        # "Framework options" in --help; separate from -p/--plugin, -m/
        # --module-path and -y/--yaml). Traced through the installed Ruby
        # source: only pre-selects an existing indexed module name / switches
        # workspace (no arbitrary-path module_eval found reachable this way),
        # but it is an unaudited, caller-controlled config-file load on an
        # already-heavily-abused flag letter; refused defensively rather than
        # left open pending a deeper trace. Case-SENSITIVE ("-c", not "-C"):
        # confirmed no legitimate msfconsole flag begins with lowercase "-c".
        if a.startswith("-c") or low.startswith("--config"):
            return ("msfconsole -c/--config loads a caller-controlled Framework "
                     "config file (module/workspace selection); refused")
    return None


def _msfvenom_reason(args: list[str]) -> str | None:
    # msfvenom's `-p` is `--payload` (module selection: core, mandatory, expected
    # usage for every invocation) — NOT msfconsole's `-p/--plugin`. msfvenom has
    # no plugin-loading flag at all (checked against the installed binary's own
    # --help); do not share msfconsole's denier here.
    return None


def _ffuf_reason(args: list[str]) -> str | None:
    # ffuf's flag parser (Go stdlib `flag`) treats a single or double leading
    # dash identically. Round-4: it DOES support `-name=value` (confirmed live
    # against the installed binary running the attacker command through
    # -input-cmd=<cmd>) even though it takes no abbreviations and has no
    # attached-without-separator form — an exact-token-only check missed the
    # `=` spelling. Match the flag name as an exact token OR as its `=`-prefix.
    for a in args:
        low = a.lower()
        for name in ("-input-cmd", "--input-cmd", "-input-shell", "--input-shell"):
            if low == name or low.startswith(name + "="):
                return ("ffuf -input-cmd runs a host shell command to generate "
                         "fuzz input; refused")
    return None


def _nikto_reason(args: list[str]) -> str | None:
    # nikto (Perl Getopt::Long) auto-abbreviates unambiguous option prefixes —
    # verified against the installed binary that `-conf`/`-con`/`-co` all resolve
    # to `-config`. Round-4: it ALSO accepts the value attached with `=`
    # (`-config=<file>`, including on an abbreviated prefix) — confirmed live,
    # which the abbreviation check alone did not cover since "config=/x" is not
    # a prefix of the literal string "config". Split on "=" first so the
    # abbreviation check sees only the flag name, not the value.
    for a in args:
        head = a.lower().split("=", 1)[0].lstrip("-")
        if len(head) >= 2 and "config".startswith(head):
            return ("nikto -config (or an abbreviation of it) can redirect "
                     "PLUGINDIR to an attacker-controlled directory, causing "
                     "arbitrary Perl execution on startup; refused")
    return None


def _john_reason(args: list[str]) -> str | None:
    # Round-5: `--config=<file>` lets the caller supply an entirely
    # attacker-authored config file carrying a `[List.External:MODE]` section;
    # `--external=<mode>` then selects it. John's own shipped docs
    # (EXTERNAL.gz) state external-mode programs and config files in general
    # are TRUSTED input the interpreter compiles and runs — confirmed live
    # (a synthetic external-mode `generate()` ran on `--stdout`). John's own
    # parser only accepts the `=`-attached form for these options (a bare
    # space-separated value is a parse error on the real binary) and DOES
    # auto-abbreviate unambiguous prefixes (confirmed live: `--con=`/`--ext=`
    # both resolve) — same split-on-"=" abbreviation-prefix check as nikto.
    # "external" and "config" diverge from every other john long option by
    # their 2nd character, so the >= 2 threshold has no real ambiguity; an
    # abbreviation john itself would reject as ambiguous costs nothing to
    # refuse pre-emptively.
    for a in args:
        head = a.lower().split("=", 1)[0].lstrip("-")
        if len(head) >= 2 and ("config".startswith(head)
                                or "external".startswith(head)):
            return ("john --config/--external loads a config file whose "
                     "[List.External:MODE] section is compiled and executed as "
                     "trusted input; refused")
    return None


def _hashcat_reason(args: list[str]) -> str | None:
    # Round-7: `--bridge-parameter1..4` (GNU long-opt style, confirmed live to
    # accept both space-separated and `=`-attached forms) overrides which
    # Python file hashcat's v7 Assimilation Bridge (`-m 72000`/`73000`)
    # `import`s — all of the file's top-level code runs before any cracking
    # attempt. Refusing the four parameter flags closes the vector without
    # blocking legitimate use of hashcat's own bundled bridges (mode
    # 72000/73000 with no caller-supplied plugin path is untouched).
    for a in args:
        low = a.lower()
        if low in ("--bridge-parameter1", "--bridge-parameter2",
                    "--bridge-parameter3", "--bridge-parameter4") or any(
                low.startswith(f"--bridge-parameter{n}=") for n in "1234"):
            return ("hashcat --bridge-parameter1..4 overrides the Python file "
                     "the Assimilation Bridge imports and runs; refused")
    return None


_LAB_ARG_POLICIES = {
    "tcpdump": _tcpdump_reason,
    "tshark": _tshark_reason,
    "sqlmap": _sqlmap_reason,
    "msfconsole": _msfconsole_reason,
    "msfvenom": _msfvenom_reason,
    "john": _john_reason,
    "ffuf": _ffuf_reason,
    "nikto": _nikto_reason,
    "hashcat": _hashcat_reason,
}


def classify(binary: str) -> CommandCapability:
    """The capability class of *binary* (basename). Unknown → REMOVED (deny)."""
    return HOST_COMMAND_POLICY.get(_basename(binary), CommandCapability.REMOVED)


def command_refusal(argv: list[str], *, lab: bool = False) -> str | None:
    """Return a refusal reason if *argv* would (or could) execute caller-controlled
    code/commands through the host gateway, else ``None``.

    This is the single command-semantic authority the executor consults after the
    allowlist + metacharacter + system-path checks. It never GRANTS anything the
    allowlist did not; it only refuses. ``lab=True`` governs a trusted-lab-only
    binary with the LAB policy (Round-2 F1) — lab tools are NOT blanket-exempt.
    """
    if not argv:
        return None
    binary = _basename(argv[0])
    args = argv[1:]

    # 0) Cross-tool command-injection fragments, whatever the binary or mode.
    for a in args:
        low = a.lower()
        for frag in _GLOBAL_INJECTION_FRAGMENTS:
            if frag in low:
                return (f"argument {a!r} carries a command-execution option "
                        f"({frag!r}); refused — use code_execute (SANDBOX_REQUIRED)")

    if lab:
        cap = LAB_COMMAND_POLICY.get(binary)
        if cap is None:
            return (f"'{binary}' is not a recognised trusted-lab tool; refused "
                    "(fail closed)")
        if cap is CommandCapability.PURPOSE_BUILT_AUTHORIZED:
            return None
        policy = _LAB_ARG_POLICIES.get(binary)
        return policy(args) if policy is not None else None

    cap = HOST_COMMAND_POLICY.get(binary, CommandCapability.REMOVED)

    if cap is CommandCapability.SAFE_FIXED_DIAGNOSTIC:
        return None

    if cap is CommandCapability.REMOVED:
        return (f"'{binary}' is not permitted in the host command gateway "
                "(it is a code/command-execution primitive); use code_execute "
                "(SANDBOX_REQUIRED) for arbitrary code")

    if cap is CommandCapability.EXECUTION_CAPABLE:
        # An interpreter/build tool: only purely informational flags are safe.
        if args and all(a.lower() in _INFO_FLAGS for a in args):
            return None
        return (f"'{binary}' with code/script/module/recipe arguments is arbitrary "
                "code execution and is not permitted from the host command gateway "
                "(HITL is authority, not containment). Use code_execute, which runs "
                "under the SANDBOX_REQUIRED ContainmentBroker")

    # SAFE_WITH_ARGUMENT_POLICY
    policy = _ARG_POLICIES.get(binary)
    if policy is None:                       # pragma: no cover - misconfiguration
        return f"'{binary}' has no argument policy; refused (fail closed)"
    return policy(args)


# Back-compat shim: the executor's earlier name. Interpreter/EXECUTION_CAPABLE
# refusals flow through the unified policy now.
def forbidden_interpreter_exec(argv: list[str]) -> str | None:
    binary = _basename(argv[0]) if argv else ""
    if HOST_COMMAND_POLICY.get(binary) is not CommandCapability.EXECUTION_CAPABLE:
        return None
    return command_refusal(argv)
