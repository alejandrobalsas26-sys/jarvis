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
    # is harmless, so it is deliberately NOT matched here.)
    for a in args:
        low = a.lower()
        if a == "-K" or low == "--config" or low.startswith("--config="):
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
# five with a generic-host-exec option carry a lab argument denier.
_LAB_PURPOSE_BUILT: frozenset[str] = frozenset({
    "masscan", "nikto", "hydra", "gobuster", "ffuf", "dirb", "sliver",
    "responder", "crackmapexec", "hashcat", "john",
})
_LAB_ARGUMENT_POLICY: frozenset[str] = frozenset({
    "tcpdump", "tshark", "sqlmap", "msfconsole", "msfvenom",
})
LAB_COMMAND_POLICY: dict[str, CommandCapability] = {
    b: CommandCapability.PURPOSE_BUILT_AUTHORIZED for b in _LAB_PURPOSE_BUILT}
LAB_COMMAND_POLICY.update(
    {b: CommandCapability.SAFE_WITH_ARGUMENT_POLICY for b in _LAB_ARGUMENT_POLICY})


def _tcpdump_reason(args: list[str]) -> str | None:
    for a in args:
        if a.lower() in ("-z", "--postrotate-command"):
            return "tcpdump -z runs a host command on each rotation; refused"
    return None


def _tshark_reason(args: list[str]) -> str | None:
    for a in args:
        low = a.lower()
        if low == "-x" or low.startswith("-x") or "lua_script" in low:
            return "tshark -X extension / Lua scripting executes host code; refused"
    return None


def _sqlmap_reason(args: list[str]) -> str | None:
    for a in args:
        low = a.lower()
        if (low in ("--eval", "--alert") or low.startswith("--eval=")
                or low.startswith("--alert=")):
            return "sqlmap --eval/--alert evaluates/runs host code; refused"
    return None


def _msf_reason(args: list[str]) -> str | None:
    for a in args:
        if a.lower() in ("-x", "-r", "--resource"):
            return ("msfconsole -x/-r runs arbitrary console/resource commands "
                    "(e.g. irb = host Ruby shell); refused")
    return None


_LAB_ARG_POLICIES = {
    "tcpdump": _tcpdump_reason,
    "tshark": _tshark_reason,
    "sqlmap": _sqlmap_reason,
    "msfconsole": _msf_reason,
    "msfvenom": _msf_reason,
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
