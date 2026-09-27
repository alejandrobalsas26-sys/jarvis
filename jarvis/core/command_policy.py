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
#:
#: Round-13 (fresh independent review, gen51 frozen candidate): the bare token
#: "version" used to live here for `npm version`, but a bare non-option token is
#: a FILENAME/GOAL to every other EXECUTION_CAPABLE binary — `python3 version`
#: and `node version` run `./version`, and `make version` runs the `version:`
#: recipe of a cwd Makefile through /bin/sh (all confirmed live). Since
#: `_sandbox_allowed_dirs()` includes `Path.cwd()` and neither shell gateway
#: overrides `subprocess.run`'s cwd, `write_file` could plant that file: the
#: same two-innocuous-approvals shape as the Round-6 `.git/config` finding, and
#: it walked straight past the check that refuses `python -c`. Bare tokens are
#: no longer informational for anyone; npm's spelling is handled separately.
_INFO_FLAGS: frozenset[str] = frozenset({
    "--version", "-v", "-V", "--help", "-h", "--info",
})

#: The one binary for which a bare `version` token is a SUBCOMMAND rather than a
#: script/goal name. Verified against the installed npm: with planted
#: package.json lifecycle scripts and .npmrc, none of its permitted spellings
#: runs anything.
_BARE_SUBCOMMAND_INFO: dict[str, frozenset[str]] = {"npm": frozenset({"version"})}

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
        # Round-15 (fresh independent review, gen53 frozen candidate): requiring a
        # read flag to be PRESENT is not the same as requiring a write action to be
        # ABSENT. `git config --replace-all core.fsmonitor <cmd> --list` satisfied
        # the old check and WROTE .git/config, because the legacy form takes
        # `name value [value-pattern]` and swallows the trailing read flag as the
        # optional value-pattern positional — confirmed live (rc=0, config
        # modified, and the next ordinary git command then ran the fsmonitor hook
        # through /bin/sh). That reopened the Round-6 primitive without write_file
        # (so _VCS_METADATA_DIRS never applied) and without git_query (so
        # Round-14's routing never applied). The other write actions happened to
        # fail on git's own argc rules, which is the tool saving us rather than
        # the policy, so every write action is now refused independently of which
        # read flags accompany it. git config DOES abbreviate its subcommand
        # options (`--rep` writes — confirmed live), so this matches
        # prefix-of-canonical, the direction used throughout this module. No
        # read-only config option is a prefix of a write-action name, so the
        # legitimate read surface (--get/--get-all/--get-regexp/--list/-l plus
        # --local/--global/--show-origin/--type/--all/--regexp/--url…) is intact.
        _WRITE_ACTIONS = ("add", "replace-all", "unset", "unset-all",
                          "remove-section", "rename-section", "edit")
        for r in rest:
            head = r.split("=", 1)[0].lstrip("-")
            if r.startswith("-") and head and any(
                    name.startswith(head) for name in _WRITE_ACTIONS):
                return ("git config write action (--add/--replace-all/--unset/"
                        "--unset-all/--remove-section/--rename-section/--edit, "
                        "any abbreviation) is refused via the host gateway")
        # New-style subcommand form: `git config set|unset|…  <key> <value>`.
        first_positional = next((r for r in rest if not r.startswith("-")), None)
        if first_positional in ("set", "unset", "remove-section",
                                 "rename-section", "edit"):
            return (f"git config {first_positional} writes configuration; "
                    "refused via the host gateway")
        # Round-16 (fresh independent review, gen54 frozen candidate): the
        # Round-15 test above only inspects tokens that start with "-", but
        # git's LEGACY IMPLICIT SET form carries no action flag at all — the
        # write is expressed purely in positionals (`git config <name> <value>`).
        # It therefore reached the present-good predicate below, which a read
        # flag placed AFTER the positionals satisfies, because git parses with
        # PARSE_OPT_STOP_AT_NON_OPTION and swallows that flag as the optional
        # [value-pattern] third positional. Confirmed live: `git config
        # core.fsmonitor <cmd> --list` returns 0, writes .git/config, and the
        # next ordinary git command runs the hook through /bin/sh — and with
        # `--global` it writes $HOME/.gitconfig, outside every sandbox root.
        #
        # Requiring "absence of write" for an action-less form means bounding
        # the ORDER: a read action must appear BEFORE any positional. Every
        # write spelling puts its positionals first (git itself rejects the
        # flag-first write orderings with "wrong number of arguments"), while
        # every legitimate read names its action first — `--get <name>`,
        # `--get-all <name>`, `--get-regexp <re>`, `--get-urlmatch <s> <url>`,
        # `--list`, `-l`, with `--local`/`--global`/`--show-origin`/`--type=`/
        # `--name-only` in any position. So this is checked by position, not by
        # counting positionals (which would wrongly refuse `--get-urlmatch`'s
        # two operands and `-f <file> <name>`).
        _READ_ACTIONS = ("--get", "--list", "-l", "--get-all", "--get-regexp",
                         "--get-urlmatch")
        read_at = next((i for i, r in enumerate(rest) if r in _READ_ACTIONS), None)
        pos_at = next((i for i, r in enumerate(rest) if not r.startswith("-")), None)
        if read_at is None:
            return "git config may only READ (--get/--list) via the host gateway"
        if pos_at is not None and pos_at < read_at:
            return ("git config with operands before the read action is a WRITE "
                    "(the legacy implicit-set form swallows a trailing read flag "
                    "as its value-pattern); refused via the host gateway")
    # Round-16: the grammar above is per-SUBCOMMAND, so ref-WRITING actions of
    # otherwise-readable subcommands were reachable — `git branch -c/--copy`,
    # `--create-reflog`, `--edit-description`, `remote remove`, `tag <name>`,
    # `reflog expire/delete`, `symbolic-ref` writes. Those mutate `.git/`, the
    # directory `_resolve_within_allowed` refuses for every file-taking handler,
    # and they were reachable from `git_query`, which is declared READ_ONLY and
    # HITL-EXEMPT. Refused per ACTION. `-c` is deliberately NOT banned globally:
    # `git log -c` is a legitimate combined-diff read.
    # Round-17 (fresh independent review, gen55 frozen candidate): the table
    # below was matched EXACT-or-`=` (`r == banned or r.startswith(banned+"=")`),
    # the direction this module abandoned in Round-9 — while git's parse-options
    # abbreviates every unambiguous long option for `branch` and `symbolic-ref`
    # too. The bare-operand fallback further down masked most abbreviations by
    # accident (an abbreviated `--dele x` still leaves `x` as a positional), so
    # exactly the write actions taking NO required operand escaped. Confirmed
    # live against git 2.53.0: `--edit-desc` wrote branch.<name>.description AND
    # executed the ambient GIT_EDITOR, `--set-upstream-t=master` wrote
    # branch.<name>.remote/.merge, `--unset-u` removed them, and
    # `symbolic-ref --del` deleted the ref — every one of them reachable from
    # git_query, which is declared READ_ONLY and HITL-EXEMPT (zero approvals).
    # Shorts stay EXACT and case-sensitive, so `-c` cannot acquire `-C`'s
    # meaning and `git log -c` stays a legitimate combined-diff read; longs match
    # prefix-of-canonical. An abbreviation ambiguous between a write and a read
    # (`--f` for --force/--format) is refused, which loses no working read: git
    # itself errors on an ambiguous abbreviation.
    _WRITE_SHORT_BY_SUBCOMMAND: dict[str, tuple[str, ...]] = {
        "branch": ("-c", "-C", "-m", "-M", "-d", "-D", "-u", "-f"),
        "tag": ("-d", "-a", "-s", "-m", "-F", "-f"),
        "symbolic-ref": ("-d",),
    }
    _WRITE_LONG_BY_SUBCOMMAND: dict[str, tuple[str, ...]] = {
        "branch": ("copy", "move", "delete", "create-reflog",
                   "edit-description", "set-upstream", "set-upstream-to",
                   "unset-upstream", "force"),
        "tag": ("delete", "annotate", "sign", "message", "file", "force",
                "create-reflog"),
        "symbolic-ref": ("delete",),
    }
    for banned in _WRITE_SHORT_BY_SUBCOMMAND.get(subcommand, ()):
        if banned in rest:
            return (f"git {subcommand} {banned} writes refs/metadata; refused "
                    "via the host gateway")
    _write_longs = _WRITE_LONG_BY_SUBCOMMAND.get(subcommand, ())
    for r in rest:
        if not r.startswith("--"):
            continue
        head = r[2:].split("=", 1)[0]
        if head and any(name.startswith(head) for name in _write_longs):
            return (f"git {subcommand} write action --{head} (an abbreviation of "
                    "a ref/metadata write) is refused via the host gateway")
    # Subcommands whose write behaviour is a bare SUB-ACTION or a bare operand.
    if subcommand in ("branch", "tag", "remote", "reflog", "symbolic-ref"):
        positionals = [r for r in rest if not r.startswith("-")]
        if subcommand == "remote":
            if positionals and positionals[0] not in ("show", "get-url"):
                return ("git remote sub-action other than show/get-url mutates "
                        "remotes; refused via the host gateway")
        elif subcommand == "reflog":
            if positionals and positionals[0] != "show":
                return ("git reflog sub-action other than show mutates reflogs; "
                        "refused via the host gateway")
        elif subcommand == "symbolic-ref":
            if len(positionals) > 1:
                return "git symbolic-ref with a value writes a ref; refused"
        else:
            # `git branch <name>` CREATES and `git tag <name>` CREATES. A bare
            # operand is only a read when an explicit read selector is present
            # (--list/--contains/--points-at/--merged/--sort/--format/…).
            # Round-17: the write grammar above is abbreviation-aware, so this
            # selector list must be too — otherwise PARSER ALIASES CHANGE POLICY
            # in the other direction and a legitimate abbreviated read
            # (`git branch --con HEAD`, `git tag --merge HEAD`) is refused as a
            # create. This direction fails CLOSED, so it was never a containment
            # defect, but the asymmetry is exactly the shape this module exists
            # to remove. Matching here cannot widen the write surface: any token
            # that is a prefix of a WRITE action name has already been refused
            # above, before control reaches this line.
            _READ_SHORT = ("-l", "-a", "-r", "-v", "-vv", "-n", "-i")
            _READ_LONG = ("list", "contains", "no-contains", "merged",
                          "no-merged", "points-at", "sort", "format", "all",
                          "remotes", "verbose", "show-current", "ignore-case",
                          "column", "omit-empty")
            has_read_selector = False
            for r in rest:
                if r in _READ_SHORT:
                    has_read_selector = True
                    break
                if r.startswith("--"):
                    h = r[2:].split("=", 1)[0]
                    if h and any(name.startswith(h) for name in _READ_LONG):
                        has_read_selector = True
                        break
            if positionals and not has_read_selector:
                return (f"git {subcommand} with a bare operand creates a "
                        f"{'branch' if subcommand == 'branch' else 'tag'}; "
                        "refused via the host gateway")
    return None


def _nmap_reason(args: list[str]) -> str | None:
    # Round-12 (fresh independent review, gen50 frozen candidate): the prior
    # check matched only the DOUBLE-dash spellings, but nmap's getopt_long
    # accepts a single leading dash for long options identically — `-script=x`
    # initialises the NSE Lua engine exactly like `--script=x` (confirmed live:
    # both reach "NSE: failed to initialize the script engine"). NSE Lua has
    # os.execute/io.popen, so a caller-authored .nse is generic host code —
    # and nmap is a BASE gateway binary, so this needed no trusted-lab mode.
    # Same "spelling the parser accepts" class as the msfconsole abbreviations.
    # Strip leading dashes and split on "=" before matching, so every spelling
    # of the same flag gets the same policy.
    #
    # `script` is matched by prefix so the whole NSE family is covered
    # (--script-args/-args-file/-help/-trace/-timeout/-updatedb). `datadir` is
    # matched as a prefix-of-canonical (>= 2 chars) since it has no sibling
    # sharing its prefix, so `--datadi` etc. are unambiguous to nmap and must
    # be refused too. Deliberately NOT prefix-matching `script` down to "s":
    # `-sC`/`-sV`/`-sn` are ordinary scan-type flags that run only nmap's OWN
    # bundled scripts, which is its declared capability, not caller code.
    #
    # Round-13: `--resume <file>` replays a command line nmap PARSES OUT of the
    # given output file ("No other arguments are permitted, as Nmap parses the
    # output file to use the same ones specified previously"), so a stored
    # `--script=` reaches NSE without ever appearing in this argv — the same
    # config-indirect class already closed for sqlmap -c, nikto -config and
    # ffuf -config, but on a BASE gateway binary. Refused by prefix (>= 3, so
    # the legitimate short `-r` is untouched and `--reason` does not match).
    for a in args:
        head = a.lower().lstrip("-").split("=", 1)[0]
        if (head.startswith("script")
                or (len(head) >= 2 and "datadir".startswith(head))
                or (len(head) >= 3 and "resume".startswith(head))):
            return ("nmap NSE scripting (--script/--datadir) and --resume, which "
                    "replays a command line parsed from a file, execute code in "
                    "any dash or abbreviated spelling; refused")
    return None


def _wget_reason(args: list[str]) -> str | None:
    # Round-13 (fresh independent review, gen51 frozen candidate): these three
    # checks matched forward (`typed.startswith(canonical)`), but wget parses
    # with getopt_long, which accepts any UNAMBIGUOUS PREFIX — `--conf=`,
    # `--exec=` and `--use-a=` all reached the real flags (confirmed live), so
    # a caller-authored wgetrc naming a `use_askpass` helper ran a host program
    # with both argv tokens slash-free. Same wrong-direction class Round-10
    # fixed for msfconsole, never migrated here. The correct direction is
    # `canonical.startswith(head)`, applied to `--` forms only: a shorter
    # prefix wget itself rejects as ambiguous costs nothing to refuse, while
    # legitimate neighbours keep working because they are NOT prefixes of a
    # denied name (`--continue`/`--content-*`, `--exclude-*`, `--user*`).
    # Single-dash shorts are matched exactly so `-c` (--continue) stays usable.
    _DENIED = ("config", "execute", "use-askpass")
    for a in args:
        low = a.lower()
        if low == "-e":
            return "wget -e/--execute sets .wgetrc directives (askpass); refused"
        if low.startswith("--"):
            head = low[2:].split("=", 1)[0]
            if head and any(name.startswith(head) for name in _DENIED):
                return ("wget --config/--execute/--use-askpass (or an "
                        "abbreviation) can set or run an askpass helper; refused")
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
    #
    # Round-9 (fresh independent review, gen47 frozen candidate): sqlmap's
    # `--tamper`, `--preprocess` and `--postprocess` each take a script path
    # that sqlmap `__import__`s during init() (option.py `_setTamperingFunctions`
    # / `_setPreprocessFunctions` / `_setPostprocessFunctions`) — Python runs the
    # imported file's top-level code BEFORE sqlmap checks the required
    # tamper()/preprocess()/postprocess() function even exists. That is generic
    # host code execution outside sqlmap's target-exploitation capability.
    # argparse auto-abbreviates (`--tamp=` reaches `--tamper`), so match a
    # split-on-"=" leading-dash-stripped head that is a prefix of any of the
    # three names (>= 4 chars: "tamp"/"prep"/"post" are the shortest unambiguous
    # prefixes among sqlmap's own long options — shorter prefixes argparse would
    # itself reject as ambiguous, so refusing them costs nothing).
    #
    # Round-12: `--eval`/`--alert` were matched as full tokens only, while the
    # SAME function's tamper/preprocess/postprocess check was already
    # abbreviation-safe — an internal inconsistency. argparse accepts `--eva`
    # and `--ale` (each unambiguous: only --eval starts with "eva", only
    # --alert with "ale"), so both are folded into the prefix set below. The
    # threshold drops to 3 to cover them; shorter prefixes are ambiguous among
    # sqlmap's own options and it rejects them itself. Upstream sqlmap also
    # gates these two behind SQLMAP_UNSAFE_EVAL/_ALERT, but per this function's
    # Round-3 note the policy refuses them directly rather than relying on an
    # external tool's own gate.
    _EXEC_SCRIPT_OPTS = ("tamper", "preprocess", "postprocess", "eval", "alert")
    for a in args:
        low = a.lower()
        if (a.startswith("-c") or low.startswith("--configfile")):
            return ("sqlmap --eval/--alert (directly or via -c/--configFile) "
                     "evaluates/runs host code; refused")
        head = low.split("=", 1)[0].lstrip("-")
        if len(head) >= 3 and any(name.startswith(head) for name in _EXEC_SCRIPT_OPTS):
            return ("sqlmap --tamper/--preprocess/--postprocess/--eval/--alert "
                     "(any abbreviated spelling) import or run caller-supplied "
                     "code on the host; refused")
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
    # Case-SENSITIVE on "-m": the long forms don't collide even lowercased.
    #
    # Round-8 (fresh independent review, gen46 frozen candidate): Round-5's
    # reasoning that `-M/--migration-path` is merely "different and
    # legitimate" was WRONG — it independently verified `-m` is dangerous but
    # never checked whether `-M` is too. Traced through the installed Ruby
    # source: `-M DIRECTORY` appends to `options.database.migrations_paths`
    # (parsed_options/base.rb), forwarded to `db_manager.init_db`, and
    # `ActiveRecord::MigrationContext#migrate` (db_manager/migration.rb) LOADS
    # every pending .rb file under those paths as a side effect of ordinary
    # console startup touching `framework.db` — ActiveRecord migrations run
    # their top-level code unconditionally when loaded, before any `up`/
    # `change` method is invoked. Confirmed live: a planted migration file's
    # top-level `File.write` ran and its version was recorded in
    # schema_migrations, with no db_migrate command and no other flag beyond
    # `-M <dir>`. Same class of bug as -m/--module-path, a different Rails
    # subsystem; -M is refused too, case-sensitively (there is no legitimate
    # use of this flag in this gateway's threat model).
    # Round-6: `-c/--config` is a THIRD config-loading flag (Framework config
    # file), refused defensively. Round-8: `-M/--migration-path` LOADS caller
    # ActiveRecord migrations at startup (see below). Both governed here.
    #
    # Round-10 (fresh independent review, gen48 frozen candidate): the long-form
    # checks above used `typed.startswith(canonical)`, which only catches the
    # FULL long option. But msfconsole parses argv with Ruby's OptionParser,
    # which accepts any UNAMBIGUOUS PREFIX (`--plug`/`--reso`/`--migr`/
    # `--module-p`/`--conf`/`--exec` all resolve to their dangerous option —
    # verified live against the bundled ruby using the real option definitions).
    # The correct, abbreviation-safe direction is `canonical.startswith(head)`,
    # exactly what _nikto_reason/_john_reason/_sqlmap_reason already do; msfconsole
    # (the only lab tool using an abbreviating parser) was never migrated to it.
    # Also, `-x`'s LONG alias `--execute-command` was missing entirely. Both
    # closed below: short options keep their case-sensitive attached-form checks,
    # and every dangerous long option is matched as a prefix of its canonical
    # name. An ambiguous prefix OptionParser would itself reject (e.g. `--modu`,
    # module-count vs module-path) is over-refused here, which costs nothing.
    _EXEC_LONG = ("execute-command", "resource", "plugin", "module-path",
                  "migration-path", "config")
    for a in args:
        low = a.lower()
        # Short forms (OptionParser: attached or space-separated). Case-sensitive
        # where an uppercase letter is a DIFFERENT, examined option: -m
        # (module-path) vs -M (migration-path, also dangerous), -c (config) has
        # no dangerous -C. -x/-r/-p have no colliding uppercase short option.
        if low.startswith("-x") or low.startswith("-r"):
            return ("msfconsole -x/-r/--execute-command/--resource run arbitrary "
                    "console/resource commands (e.g. irb = host Ruby shell); refused")
        if low.startswith("-p"):
            return ("msfconsole -p/--plugin `require`s an arbitrary Ruby file on "
                     "startup, executing its top-level code before any plugin "
                     "validity check; refused")
        if a.startswith("-m"):
            return ("msfconsole -m/--module-path eagerly module_eval()s "
                     "arbitrary Ruby under the given directory with "
                     "--no-defer-module-loads; refused")
        if a.startswith("-M"):
            return ("msfconsole -M/--migration-path loads caller-supplied "
                     "ActiveRecord migration .rb files at ordinary startup, "
                     "running their top-level code; refused")
        if a.startswith("-c"):
            return ("msfconsole -c/--config loads a caller-controlled Framework "
                     "config file (module/workspace selection); refused")
        # Long forms, including OptionParser abbreviations: refuse any "--" arg
        # whose head (before "=") is a non-empty prefix of a dangerous option.
        if low.startswith("--"):
            head = low[2:].split("=", 1)[0]
            if head and any(name.startswith(head) for name in _EXEC_LONG):
                return ("msfconsole long option (or an OptionParser abbreviation) "
                         "reaching a code-loading flag "
                         "(--execute-command/--resource/--plugin/--module-path/"
                         "--migration-path/--config); refused")
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
    #
    # Round-11 (fresh independent review, gen49 frozen candidate): `-config
    # <file>` loads a TOML config that can itself set ffuf's `inputcommands`/
    # `inputshell` — the SAME host-shell-command capability the -input-cmd/
    # -input-shell denier above exists to refuse. Confirmed live: a config with
    # `[input] inputcommands = [...]` ran an arbitrary /bin/sh command at
    # startup. Same config-indirect class as sqlmap -c/--configFile (Round-3)
    # and nikto -config (Round-3); refuse the config-load vector directly.
    _EXEC = ("-input-cmd", "--input-cmd", "-input-shell", "--input-shell",
             "-config", "--config")
    for a in args:
        low = a.lower()
        for name in _EXEC:
            if low == name or low.startswith(name + "="):
                return ("ffuf -input-cmd/-input-shell run a host shell command, "
                         "and -config can set the same via a TOML file; refused")
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
    #
    # Round-14 (fresh independent review, gen52 frozen candidate): john stores
    # its ORIGINAL argv in a `.rec` session file and re-parses it on restore, so
    # `--restore=<name>` (and `--catch-up=`/`--status=`, which read the same
    # file) replay the very `--config`/`--external` pair refused above — the
    # session-REPLAY member of the config-indirect class already closed for nmap
    # --resume, sqlmap -c, ffuf -config and nikto -config. `<name>.rec` resolves
    # RELATIVE TO THE CWD, i.e. straight into a write_file-writable root, and the
    # token is slash-free so the Layer-2 path guard never sees it.
    for a in args:
        head = a.lower().split("=", 1)[0].lstrip("-")
        if len(head) >= 2 and ("config".startswith(head)
                                or "external".startswith(head)):
            return ("john --config/--external loads a config file whose "
                     "[List.External:MODE] section is compiled and executed as "
                     "trusted input; refused")
        if len(head) >= 3 and ("restore".startswith(head)
                                or "catch-up".startswith(head)
                                or "status".startswith(head)):
            return ("john --restore/--catch-up/--status replay a .rec session "
                     "file that carries the original argv, reinstating "
                     "--config/--external; refused")
    return None


def _hashcat_reason(args: list[str]) -> str | None:
    # Round-7: `--bridge-parameter1..4` (GNU long-opt style, confirmed live to
    # accept both space-separated and `=`-attached forms) overrides which
    # Python file hashcat's v7 Assimilation Bridge (`-m 72000`/`73000`)
    # `import`s — all of the file's top-level code runs before any cracking
    # attempt. Refusing the four parameter flags closes the vector without
    # blocking legitimate use of hashcat's own bundled bridges (mode
    # 72000/73000 with no caller-supplied plugin path is untouched).
    #
    # Round-14: hashcat likewise stores its original argv in a `.restore`
    # session file and re-parses it, so `--restore` (with or without
    # `--restore-file-path=<file>`) replays the bridge-parameter flags refused
    # above — proven by a restore file carrying a bogus flag, which hashcat
    # reported as `unrecognized option`. The restore file is pure ASCII, so
    # write_file plants it verbatim in the inherited cwd, and the path token is
    # slash-free. Matched prefix-of-canonical (>= 3): `--restore-disable`, which
    # only SUPPRESSES the session file, is deliberately NOT caught, and
    # `--remove`/`--runtime` are not prefixes of either denied name.
    _REPLAY = ("restore", "restore-file-path")
    for a in args:
        low = a.lower()
        if low in ("--bridge-parameter1", "--bridge-parameter2",
                    "--bridge-parameter3", "--bridge-parameter4") or any(
                low.startswith(f"--bridge-parameter{n}=") for n in "1234"):
            return ("hashcat --bridge-parameter1..4 overrides the Python file "
                     "the Assimilation Bridge imports and runs; refused")
        head = low.split("=", 1)[0].lstrip("-")
        if len(head) >= 3 and any(name.startswith(head) for name in _REPLAY):
            return ("hashcat --restore/--restore-file-path replay a session file "
                     "that carries the original argv, reinstating "
                     "--bridge-parameter1..4; refused")
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
        # Round-13: a bare non-option token is a script/goal name to python,
        # node and make, so it is informational ONLY for the binary that
        # declares it as a subcommand (see _BARE_SUBCOMMAND_INFO).
        allowed = _INFO_FLAGS | _BARE_SUBCOMMAND_INFO.get(binary, frozenset())
        if args and all(a.lower() in allowed for a in args):
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
