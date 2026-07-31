"""training_gym/policies.py — V69 M62: the bounds an episode may not exceed.

WHY THIS EXISTS
---------------
A task specification is written by a human and read by a sandbox launcher. Between
those two points sits every mistake this module exists to make impossible: a task
that asks for 64 GB, a task that asks for the network "just for pip", a task whose
artifact allowlist is ``*``, a task that asks for a two-hour wall clock inside a CI
job. Those are not runtime failures — they are *specification* failures, and they
must be rejected when the spec is parsed, before any container exists.

So every policy here is a frozen dataclass with a hard, non-negotiable ceiling
enforced in ``__post_init__``. A policy object that exists is a policy that is
already within bounds; no consumer needs to re-check it, and no consumer can widen
it. Widening requires editing the ceilings in this file, in a diff a reviewer sees.

THE FAIL-OPEN PATTERNS THIS FILE DELIBERATELY AVOIDS
----------------------------------------------------
  * ``bool(payload.get(...))`` — ``bool("false")`` is ``True`` and ``bool(0)`` is
    ``False``, so a JSON policy could be read as the opposite of what it states, in
    either direction. Every boolean goes through :func:`~training_gym.schemas.
    require_bool`, which accepts only unambiguous values;
  * ``require_str_tuple(...) or DEFAULT`` — an explicit empty list means "nothing",
    not "everything the default allows". Absence and emptiness are distinguished with
    a sentinel;
  * exact-string denylists — ``seccomp="unconfined"`` was refused while
    ``"Unconfined"`` was accepted. Where the safe set is enumerable, this file uses an
    ALLOWLIST, so a value nobody anticipated is refused rather than admitted;
  * substring path matching — ``"/dev" in "/srv/devtools"`` is a false rejection and
    ``"/home/alice"`` was not a rejection at all. Mount sources are compared by
    normalised path SEGMENT.

DISCIPLINE
----------
  * pure stdlib, no I/O, no clock;
  * ceilings are module constants with the reason in a comment, not magic numbers;
  * ``NetworkPolicy.NONE`` is the default everywhere it appears, and the permissive
    variant carries an explicit destination allowlist of real fully-qualified
    hostnames — there is no "allow all" mode and no wildcard that could become one.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import ClassVar

from .schemas import (
    SchemaError,
    require_bool,
    require_float,
    require_id,
    require_int,
    require_str_tuple,
)

# ── hard ceilings ─────────────────────────────────────────────────────────────
# A single practice episode must stay something an operator can run on a laptop
# between two commands. These are deliberately small: the gym validates a learning
# factory, it is not a build farm.
MAX_TIMEOUT_S = 900            # 15 min wall clock for a whole episode
MAX_COMMAND_TIMEOUT_S = 300    # 5 min for any single command inside it
MAX_MEMORY_MB = 4096           # 4 GB — fits beside a running Ollama on a 16 GB host
MAX_CPUS = 2.0                 # never take the whole machine
MAX_PIDS = 256                 # fork-bomb ceiling
MAX_OUTPUT_BYTES = 1_048_576   # 1 MiB of captured stdout+stderr per command
MAX_FILE_SIZE_BYTES = 8_388_608   # 8 MiB per produced artifact
MAX_ARTIFACT_COUNT = 64
MAX_ARTIFACT_PATTERNS = 32
MAX_WORKSPACE_FILES = 512      # fixtures copied into a fresh workspace
MAX_NETWORK_DESTINATIONS = 16
MAX_MANDATORY_GRADERS = 32

#: Environment variables a sandboxed episode may see. Everything else is dropped —
#: this is an allowlist, so a newly-invented credential variable is excluded by
#: default rather than requiring someone to remember to deny it. A task may narrow
#: this set but may NOT extend it: ``LD_PRELOAD``, ``PYTHONPATH``, ``HTTP_PROXY`` and
#: ``GIT_SSH_COMMAND`` are code-injection and egress channels that no credential-name
#: pattern would ever catch.
DEFAULT_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH", "HOME", "LANG", "LC_ALL", "TZ", "PYTHONHASHSEED",
    "PYTHONDONTWRITEBYTECODE", "PYTHONUNBUFFERED", "TMPDIR",
)

#: Fixed, so two runs of the same task sort, format and seed identically. ``HOME`` and
#: ``TMPDIR`` are pinned rather than inherited: the operator's home path contains the
#: operator's username, which must never reach a container or a recorded environment.
DETERMINISTIC_ENV: dict[str, str] = {
    "HOME": "/home/gym",
    "TMPDIR": "/tmp",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TZ": "UTC",
    "PYTHONHASHSEED": "0",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONUNBUFFERED": "1",
}

#: Whole ``_``-delimited tokens that make an environment variable name
#: credential-shaped. Token matching, not substring matching, so ``MONKEY`` is not
#: rejected for containing ``KEY`` and ``AUTHOR`` is not rejected for containing
#: ``AUTH`` — a validator that cries wolf gets disabled.
_CREDENTIAL_TOKENS: frozenset[str] = frozenset({
    "KEY", "KEYS", "APIKEY", "TOKEN", "TOKENS", "SECRET", "SECRETS",
    "PASSWORD", "PASSWD", "PWD", "PASS", "CREDENTIAL", "CREDENTIALS", "CRED",
    "AUTH", "SESSION", "COOKIE", "SIGNATURE", "PRIVATE", "CERT", "PEM",
    "NETRC", "DSN", "LICENSE",
})

#: Seccomp profiles the gym accepts. An ALLOWLIST, because the set of safe values is
#: small and knowable while the set of dangerous ones is not: ``"unconfined"``,
#: ``"Unconfined"``, ``" unconfined "`` and an attacker-supplied profile path all
#: fall outside it without anyone having to enumerate them.
ALLOWED_SECCOMP_PROFILES: frozenset[str] = frozenset({"default", "runtime/default"})

#: Path SEGMENTS that make a mount source illegal wherever they appear.
_FORBIDDEN_SEGMENTS: frozenset[str] = frozenset({
    ".ssh", ".aws", ".azure", ".gcloud", ".kube", ".docker", ".gnupg",
    ".netrc", ".git-credentials", ".ollama", ".password-store", ".mozilla",
    ".cache", ".config", "appdata", "id_rsa", "shadow", "credentials",
})

#: First path segments that are operating-system or credential territory, never
#: episode material.
_FORBIDDEN_ROOT_SEGMENTS: frozenset[str] = frozenset({
    "proc", "sys", "dev", "etc", "root", "boot", "windows", "winnt",
    "program files", "program files (x86)", "programdata", "system32",
})

#: First path segments that begin a user's home tree on any supported platform.
_HOME_ROOT_SEGMENTS: frozenset[str] = frozenset({"home", "users"})

#: Public DNS services that resolve an arbitrary label to an arbitrary address —
#: ``127.0.0.1.nip.io`` is a well-formed public FQDN pointing at loopback.
_REBINDING_SUFFIXES: tuple[str, ...] = (
    ".nip.io", ".xip.io", ".sslip.io", ".localtest.me", ".traefik.me",
    ".vcap.me", ".lvh.me", ".127.0.0.1.org",
)

_FQDN_RE = re.compile(
    r"^(?!-)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.(?!-)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
_DRIVE_RE = re.compile(r"^([a-z]):(/.*)?$")
_GLOB_CHARS = "*?[]"


class NetworkPolicy(str, Enum):
    """What an episode may reach. ``NONE`` is the default everywhere.

    There is deliberately no ``ANY``: the permissive value is ``ALLOWLIST``, which is
    meaningless without a non-empty destination list, so "open the network" cannot be
    expressed by flipping one enum member.
    """

    NONE = "none"                  # no interfaces beyond loopback-in-container
    ALLOWLIST = "allowlist"        # only the named destinations, via a proxy design

    @property
    def enabled(self) -> bool:
        return self is not NetworkPolicy.NONE


def _is_private_destination(host: str) -> bool:
    """True for anything on the operator's own network or cloud metadata.

    A task's "external" destination must be genuinely external. Loopback, RFC1918,
    link-local (which is where 169.254.169.254 lives), CGNAT and ``*.local`` are all
    ways to point an allowlist back at the host that is supposed to be isolated.
    """
    name = (host or "").strip().lower().rstrip(".")
    if not name:
        return True
    if name in ("localhost", "host.docker.internal", "gateway.docker.internal",
                "metadata.google.internal", "metadata", "instance-data"):
        return True
    if name.endswith((".local", ".internal", ".localdomain", ".home.arpa")):
        return True
    if name.endswith(_REBINDING_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(name)
    except ValueError:
        return False
    return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _require_destination(raw: object) -> str:
    """One allowlist entry: a bare, fully-qualified, wildcard-free public hostname.

    The FQDN requirement is the control that closes the "allow everything" hole. A
    bare ``*`` contains no scheme, no slash and does not parse as an IP address, so a
    check built only from those tests would have accepted it as an ordinary hostname
    and the proxy would have matched every destination in existence.
    """
    if not isinstance(raw, str):
        raise SchemaError(f"network.destinations: expected a hostname string, got "
                          f"{type(raw).__name__}")
    host = raw.strip().lower().rstrip(".")
    if not host:
        raise SchemaError("network.destinations: empty destination")
    if "://" in host or "/" in host:
        raise SchemaError(f"network: destination must be a bare host, got {raw!r}")
    if any(ch in host for ch in "*?"):
        raise SchemaError(f"network: wildcard destination {raw!r} is refused — it is "
                          f"an allow-everything rule written as a hostname")
    if ":" in host:
        raise SchemaError(f"network: destination must not carry a port or scheme, "
                          f"got {raw!r}")
    if not _FQDN_RE.match(host):
        raise SchemaError(f"network: destination {raw!r} is not a fully-qualified "
                          f"public hostname")
    if _is_private_destination(host):
        raise SchemaError(f"network: destination {raw!r} resolves to the host's own "
                          f"network / metadata range and is refused")
    return host


@dataclass(frozen=True)
class NetworkSpec:
    """The network face of a task. Defaults to fully disabled."""

    policy: NetworkPolicy = NetworkPolicy.NONE
    #: Hostnames (not URLs) reachable through the allowlisted proxy design.
    destinations: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if self.policy is NetworkPolicy.NONE:
            if self.destinations:
                raise SchemaError("network: destinations declared with policy=none")
            return
        if not self.destinations:
            raise SchemaError("network: policy=allowlist requires destinations "
                              "(there is no allow-everything mode)")
        if len(self.destinations) > MAX_NETWORK_DESTINATIONS:
            raise SchemaError(f"network: {len(self.destinations)} destinations "
                              f"exceeds the ceiling of {MAX_NETWORK_DESTINATIONS}")
        if not self.reason.strip():
            raise SchemaError("network: an enabled network policy must state a reason")
        object.__setattr__(self, "destinations",
                           tuple(_require_destination(d) for d in self.destinations))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["policy"] = self.policy.value
        d["destinations"] = list(self.destinations)
        return d

    @classmethod
    def from_dict(cls, payload: dict | None) -> "NetworkSpec":
        if not payload:
            return cls()
        from .schemas import reject_unknown_fields, require_enum
        reject_unknown_fields(payload, ("policy", "destinations", "reason"),
                              label="network")
        return cls(
            policy=require_enum(payload.get("policy", NetworkPolicy.NONE.value),
                                NetworkPolicy, "network.policy"),
            destinations=require_str_tuple(payload.get("destinations"),
                                           "network.destinations",
                                           max_items=MAX_NETWORK_DESTINATIONS),
            reason=str(payload.get("reason", "")),
        )


@dataclass(frozen=True)
class ResourceBudget:
    """Wall clock, CPU, memory and output ceilings for one episode."""

    timeout_s: int = 120
    command_timeout_s: int = 60
    memory_mb: int = 1024
    cpus: float = 1.0
    pids: int = 128
    max_output_bytes: int = 262_144
    max_file_size_bytes: int = 1_048_576

    def __post_init__(self) -> None:
        checks = (
            ("timeout_s", self.timeout_s, 1, MAX_TIMEOUT_S),
            ("command_timeout_s", self.command_timeout_s, 1, MAX_COMMAND_TIMEOUT_S),
            ("memory_mb", self.memory_mb, 64, MAX_MEMORY_MB),
            ("pids", self.pids, 1, MAX_PIDS),
            ("max_output_bytes", self.max_output_bytes, 1024, MAX_OUTPUT_BYTES),
            ("max_file_size_bytes", self.max_file_size_bytes, 1024, MAX_FILE_SIZE_BYTES),
        )
        for name, value, low, high in checks:
            # ``isinstance(True, int)`` is True, so bool is excluded explicitly: a
            # budget of ``memory_mb: true`` would otherwise become 1 MB.
            if not isinstance(value, int) or isinstance(value, bool):
                raise SchemaError(f"resources.{name}: expected an integer, got "
                                  f"{type(value).__name__}")
            if not (low <= value <= high):
                raise SchemaError(f"resources.{name}: {value} outside [{low}, {high}]")
        if isinstance(self.cpus, bool) or not isinstance(self.cpus, (int, float)):
            raise SchemaError("resources.cpus: expected a number")
        if not (0.1 <= float(self.cpus) <= MAX_CPUS):
            raise SchemaError(f"resources.cpus: {self.cpus} outside [0.1, {MAX_CPUS}]")
        if self.command_timeout_s > self.timeout_s:
            raise SchemaError("resources: command_timeout_s exceeds the episode timeout_s")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict | None) -> "ResourceBudget":
        if not payload:
            return cls()
        from .schemas import reject_unknown_fields
        int_fields = ("timeout_s", "command_timeout_s", "memory_mb", "pids",
                      "max_output_bytes", "max_file_size_bytes")
        reject_unknown_fields(payload, (*int_fields, "cpus"), label="resources")
        defaults = cls()
        kwargs: dict[str, object] = {}
        for name in int_fields:
            # Routed through require_int so a malformed value raises SchemaError —
            # the gym's one error type — rather than a bare ValueError that a caller
            # catching SchemaError would not see.
            kwargs[name] = require_int(payload.get(name, getattr(defaults, name)),
                                       f"resources.{name}",
                                       minimum=0, maximum=MAX_TIMEOUT_S * 10_000)
        kwargs["cpus"] = require_float(payload.get("cpus", defaults.cpus),
                                       "resources.cpus", minimum=0.0, maximum=1024.0)
        return cls(**kwargs)  # type: ignore[arg-type]


@dataclass(frozen=True)
class ArtifactPolicy:
    """Which produced files may leave the sandbox.

    An allowlist of relative glob patterns, each of which must begin with a LITERAL
    directory segment. That single rule is what makes the policy meaningful: ``*``,
    ``*/*``, ``**/*.*`` and ``?*`` are all ways of writing "collect everything the
    episode wrote", which is how a workspace copy of the operator's fixtures, a core
    dump or a stray credential file becomes an exported artifact.
    """

    patterns: tuple[str, ...] = ("output/*.json", "output/*.txt")
    max_count: int = 16

    def __post_init__(self) -> None:
        if not self.patterns:
            raise SchemaError("artifacts: at least one allowlist pattern is required")
        if len(self.patterns) > MAX_ARTIFACT_PATTERNS:
            raise SchemaError(f"artifacts: {len(self.patterns)} patterns exceeds the "
                              f"ceiling of {MAX_ARTIFACT_PATTERNS}")
        if not isinstance(self.max_count, int) or isinstance(self.max_count, bool):
            raise SchemaError("artifacts.max_count: expected an integer")
        if not (1 <= self.max_count <= MAX_ARTIFACT_COUNT):
            raise SchemaError(f"artifacts.max_count: {self.max_count} outside "
                              f"[1, {MAX_ARTIFACT_COUNT}]")
        for pat in self.patterns:
            self._validate_pattern(pat)

    @staticmethod
    def _validate_pattern(pat: str) -> None:
        text = str(pat).strip()
        if not text:
            raise SchemaError("artifacts: empty pattern")
        if "~" in text:
            raise SchemaError(f"artifacts: pattern {pat!r} references a home directory")
        if text.startswith(("/", "\\")) or ":" in text:
            raise SchemaError(f"artifacts: pattern {pat!r} must be a relative "
                              f"in-workspace path")
        norm = text.replace("\\", "/")
        segments = norm.split("/")
        if any(seg in ("", ".", "..") for seg in segments):
            raise SchemaError(f"artifacts: pattern {pat!r} contains a traversal or "
                              f"empty segment")
        if len(segments) < 2:
            raise SchemaError(
                f"artifacts: pattern {pat!r} has no directory prefix; name the output "
                f"location explicitly, e.g. 'output/*.json'")
        head = segments[0]
        if any(ch in head for ch in _GLOB_CHARS):
            raise SchemaError(
                f"artifacts: pattern {pat!r} begins with the glob {head!r}; the first "
                f"segment must be a literal directory or the pattern collects the "
                f"whole workspace")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["patterns"] = list(self.patterns)
        return d

    @classmethod
    def from_dict(cls, payload: dict | None) -> "ArtifactPolicy":
        if not payload:
            return cls()
        from .schemas import reject_unknown_fields
        reject_unknown_fields(payload, ("patterns", "max_count"), label="artifacts")
        default = cls()
        # ``"patterns": []`` states "collect nothing", which is invalid and must be
        # REPORTED — never silently replaced by the default allowlist.
        raw = payload.get("patterns", None)
        patterns = (default.patterns if raw is None
                    else require_str_tuple(raw, "artifacts.patterns",
                                           max_items=MAX_ARTIFACT_PATTERNS))
        return cls(
            patterns=patterns,
            max_count=require_int(payload.get("max_count", default.max_count),
                                  "artifacts.max_count", minimum=0, maximum=100_000),
        )


@dataclass(frozen=True)
class MountSpec:
    """One host directory offered to a sandbox, validated at construction.

    Exists so the "never mount the Docker socket / the home directory / /proc" rule is
    a TYPE, not a helper someone downstream has to remember to call. A backend that
    accepts only :class:`MountSpec` cannot be handed a raw string.
    """

    source: str
    target: str
    read_only: bool = True

    def __post_init__(self) -> None:
        marker = forbidden_mount(self.source)
        if marker:
            raise SchemaError(f"mount: source is refused ({marker}); the sandbox "
                              f"never receives operator credentials, home content, "
                              f"OS directories or a container control socket")
        target = str(self.target or "").strip()
        if not target.startswith("/") or target.startswith("//"):
            raise SchemaError(f"mount.target: expected an absolute in-container POSIX "
                              f"path, got {self.target!r}")
        segments = [s for s in target.split("/") if s]
        if not segments:
            raise SchemaError("mount.target: refusing to mount over the container root")
        if segments[0] in _FORBIDDEN_ROOT_SEGMENTS:
            raise SchemaError(f"mount.target: refusing to mount over /{segments[0]}")
        if ".." in segments:
            raise SchemaError(f"mount.target: traversal in {self.target!r}")
        object.__setattr__(self, "read_only", require_bool(self.read_only,
                                                           "mount.read_only"))

    def to_dict(self) -> dict:
        return {"source": self.source, "target": self.target,
                "read_only": self.read_only}


@dataclass(frozen=True)
class SandboxPolicy:
    """The container-level posture. Every field defaults to the restrictive value.

    These are asserted, not merely requested: :mod:`training_gym.sandbox.security`
    refuses to build a launch command whose flags contradict them, and the negative
    controls in the test suite inspect the generated argv rather than trusting this
    object.
    """

    read_only_rootfs: bool = True
    no_new_privileges: bool = True
    drop_all_capabilities: bool = True
    run_as_non_root: bool = True
    seccomp: str = "default"          # "default" == the runtime's own profile
    tmpfs_workspace: bool = True
    persistent_volume: bool = False
    env_allowlist: tuple[str, ...] = DEFAULT_ENV_ALLOWLIST

    #: The isolation floor. A task may not lower any of these; a backend that cannot
    #: honour one must say so in its capability report, not proceed quietly.
    #: ``ClassVar`` so the dataclass machinery treats it as a constant, not a field.
    FLOOR: ClassVar[tuple[str, ...]] = (
        "no_new_privileges", "drop_all_capabilities", "run_as_non_root",
        "read_only_rootfs", "tmpfs_workspace")

    def __post_init__(self) -> None:
        for name in ("read_only_rootfs", "no_new_privileges", "drop_all_capabilities",
                     "run_as_non_root", "tmpfs_workspace", "persistent_volume"):
            object.__setattr__(self, name,
                               require_bool(getattr(self, name), f"sandbox.{name}"))
        for name in self.FLOOR:
            if not getattr(self, name):
                raise SchemaError(f"sandbox.{name}: may not be disabled")
        profile = str(self.seccomp or "").strip().lower()
        if profile not in ALLOWED_SECCOMP_PROFILES:
            raise SchemaError(
                f"sandbox.seccomp: {self.seccomp!r} is not one of "
                f"{sorted(ALLOWED_SECCOMP_PROFILES)}; an unconfined or externally "
                f"supplied profile is refused")
        object.__setattr__(self, "seccomp", profile)
        if self.persistent_volume:
            raise SchemaError("sandbox.persistent_volume: episodes are disposable; "
                              "a persistent volume is refused")
        if not self.env_allowlist:
            raise SchemaError("sandbox.env_allowlist: at least PATH is required")
        extra = sorted(set(self.env_allowlist) - set(DEFAULT_ENV_ALLOWLIST))
        if extra:
            raise SchemaError(
                f"sandbox.env_allowlist: {extra} may not be added. The allowlist can "
                f"be narrowed but never extended — LD_PRELOAD, PYTHONPATH, "
                f"HTTP_PROXY and GIT_SSH_COMMAND are code-injection and egress "
                f"channels that no credential-name pattern would catch")
        bad = sorted(v for v in self.env_allowlist if credential_shaped(v))
        if bad:
            raise SchemaError(f"sandbox.env_allowlist: credential-shaped names {bad}")

    def to_dict(self) -> dict:
        return {"read_only_rootfs": self.read_only_rootfs,
                "no_new_privileges": self.no_new_privileges,
                "drop_all_capabilities": self.drop_all_capabilities,
                "run_as_non_root": self.run_as_non_root,
                "seccomp": self.seccomp,
                "tmpfs_workspace": self.tmpfs_workspace,
                "persistent_volume": self.persistent_volume,
                "env_allowlist": sorted(self.env_allowlist)}

    @classmethod
    def from_dict(cls, payload: dict | None) -> "SandboxPolicy":
        if not payload:
            return cls()
        from .schemas import reject_unknown_fields
        fields = ("read_only_rootfs", "no_new_privileges", "drop_all_capabilities",
                  "run_as_non_root", "seccomp", "tmpfs_workspace",
                  "persistent_volume", "env_allowlist")
        reject_unknown_fields(payload, fields, label="sandbox")
        default = cls()
        raw_env = payload.get("env_allowlist", None)
        env = (default.env_allowlist if raw_env is None
               else require_str_tuple(raw_env, "sandbox.env_allowlist", max_items=32))
        return cls(
            read_only_rootfs=payload.get("read_only_rootfs", True),
            no_new_privileges=payload.get("no_new_privileges", True),
            drop_all_capabilities=payload.get("drop_all_capabilities", True),
            run_as_non_root=payload.get("run_as_non_root", True),
            seccomp=str(payload.get("seccomp", "default")),
            tmpfs_workspace=payload.get("tmpfs_workspace", True),
            persistent_volume=payload.get("persistent_volume", False),
            env_allowlist=env,
        )


@dataclass(frozen=True)
class ScoringPolicy:
    """How a task's reward dimensions combine, and what must be present to approve.

    ``mandatory_graders`` is the honesty hinge: a grader named here that returns
    anything other than PASS / NOT_APPLICABLE blocks approval, including when it
    returned SKIPPED because its tool was not installed.
    """

    mandatory_graders: tuple[str, ...] = ()
    weights: dict[str, float] = field(default_factory=dict)
    #: Minimum aggregate reward for an attempt to be dataset-eligible.
    min_total_score: float = 0.7
    #: Refusal is the correct answer for this task (unsafe / underspecified prompt).
    expect_refusal: bool = False

    def __post_init__(self) -> None:
        if not self.mandatory_graders:
            raise SchemaError("scoring.mandatory_graders: at least one deterministic "
                              "grader must be mandatory (an unmeasured attempt is "
                              "never approvable)")
        if len(self.mandatory_graders) > MAX_MANDATORY_GRADERS:
            raise SchemaError(f"scoring.mandatory_graders: too many "
                              f"({len(self.mandatory_graders)})")
        for gid in self.mandatory_graders:
            require_id(gid, "scoring.mandatory_graders[]")
        if len(set(self.mandatory_graders)) != len(self.mandatory_graders):
            raise SchemaError("scoring.mandatory_graders: duplicate grader id")
        require_float(self.min_total_score, "scoring.min_total_score",
                      minimum=0.0, maximum=1.0)
        for name, weight in self.weights.items():
            require_float(weight, f"scoring.weights[{name}]", minimum=0.0, maximum=1.0)
        object.__setattr__(self, "expect_refusal",
                           require_bool(self.expect_refusal, "scoring.expect_refusal"))

    def to_dict(self) -> dict:
        # Sorted: two policies that mandate the same graders are the same policy and
        # must produce the same task hash regardless of authoring order.
        return {"mandatory_graders": sorted(self.mandatory_graders),
                "weights": dict(sorted(self.weights.items())),
                "min_total_score": self.min_total_score,
                "expect_refusal": self.expect_refusal}

    @classmethod
    def from_dict(cls, payload: dict | None) -> "ScoringPolicy":
        from .schemas import reject_unknown_fields, require_mapping
        payload = payload or {}
        reject_unknown_fields(payload, ("mandatory_graders", "weights",
                                        "min_total_score", "expect_refusal"),
                              label="scoring")
        weights_raw = require_mapping(payload.get("weights", {}), "scoring.weights")
        return cls(
            mandatory_graders=require_str_tuple(payload.get("mandatory_graders"),
                                                "scoring.mandatory_graders",
                                                max_items=MAX_MANDATORY_GRADERS),
            weights={str(k): require_float(v, f"scoring.weights[{k}]",
                                           minimum=0.0, maximum=1.0)
                     for k, v in weights_raw.items()},
            min_total_score=require_float(payload.get("min_total_score", 0.7),
                                          "scoring.min_total_score",
                                          minimum=0.0, maximum=1.0),
            expect_refusal=payload.get("expect_refusal", False),
        )


# ── mount and environment screening ───────────────────────────────────────────
def forbidden_mount(source: str) -> str | None:
    """The reason *source* is an illegal mount, or ``None`` if it is acceptable.

    Compares normalised path SEGMENTS rather than substrings. Substring matching was
    wrong in both directions: it rejected ``/srv/devtools`` for containing ``/dev``
    and accepted ``/home/alice`` because only the bare parent ``/home`` was listed.
    """
    raw = str(source or "").strip()
    if not raw:
        return "empty source"
    text = raw.replace("\\", "/").lower().rstrip("/")

    # Windows container control channels reach the daemon without ever being a
    # "socket file", so they are checked before any path parsing.
    if text.startswith("npipe:") or "//./pipe" in text:
        return "windows named pipe (container control channel)"
    if re.match(r"^[a-z][a-z0-9+.-]*://", text):
        return "url, not a filesystem path"
    if text.startswith("//"):
        return "UNC network path"
    if text.startswith("~") or text.startswith("%") or "$" in text:
        return "unexpanded home or environment reference"

    drive = _DRIVE_RE.match(text)
    if drive:
        text = drive.group(2) or "/"
    segments = [s for s in text.split("/") if s]
    if not segments:
        return "filesystem root"
    if ".." in segments:
        return "path traversal"
    head = segments[0]
    if head in _HOME_ROOT_SEGMENTS:
        return "user home directory tree"
    if head in _FORBIDDEN_ROOT_SEGMENTS:
        return f"/{head} (operating-system directory)"
    for seg in segments:
        if seg in _FORBIDDEN_SEGMENTS:
            return f"{seg} (credential or cache directory)"
        if seg.endswith(".sock"):
            return f"{seg} (unix control socket)"
    return None


def credential_shaped(name: str) -> bool:
    """True if an environment variable NAME looks like it carries a credential.

    Token-based: ``API_KEY`` and ``MYSQL_PWD`` match, ``MONKEY`` and ``AUTHOR`` do
    not. A validator that rejects innocuous names gets switched off, so precision
    here is a security property, not a nicety.
    """
    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", str(name or "").upper()) if t]
    return any(token in _CREDENTIAL_TOKENS for token in tokens)


__all__ = [
    "ALLOWED_SECCOMP_PROFILES", "ArtifactPolicy", "DEFAULT_ENV_ALLOWLIST",
    "DETERMINISTIC_ENV", "MAX_ARTIFACT_COUNT", "MAX_ARTIFACT_PATTERNS",
    "MAX_COMMAND_TIMEOUT_S", "MAX_CPUS", "MAX_FILE_SIZE_BYTES",
    "MAX_MANDATORY_GRADERS", "MAX_MEMORY_MB", "MAX_NETWORK_DESTINATIONS",
    "MAX_OUTPUT_BYTES", "MAX_PIDS", "MAX_TIMEOUT_S", "MAX_WORKSPACE_FILES",
    "MountSpec", "NetworkPolicy", "NetworkSpec", "ResourceBudget", "SandboxPolicy",
    "ScoringPolicy", "credential_shaped", "forbidden_mount",
]
