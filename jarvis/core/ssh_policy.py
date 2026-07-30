"""core/ssh_policy.py — V69 M61.7: fail-closed SSH host-key verification.

WHY THIS EXISTS
---------------
Two modules opened SSH connections with ``paramiko.AutoAddPolicy()``
(``core/sensor_mesh.py``, ``tools/ebpf_bridge.py`` — Bandit B507, HIGH). That policy
is trust-on-first-use with no operator in the loop: the *first* machine to answer on
the target address becomes permanently trusted, silently. On the flat lab networks
these bridges are pointed at — the exact place an attacker who can answer ARP or DNS
lives — that hands a man-in-the-middle a JARVIS-authenticated SSH session, with the
operator's private key doing the authenticating.

THE POLICY
----------
  * **Fail closed.** ``paramiko.RejectPolicy`` — an unknown host key raises. A
    *mismatched* key raises ``BadHostKeyException`` from paramiko itself, before any
    policy is consulted, so key rotation and impersonation are both loud.
  * **Two host-key stores are loaded:** the operator's own system ``known_hosts``
    (``~/.ssh/known_hosts``) and a JARVIS-managed store under the managed data tree,
    so enrolling a lab VM for JARVIS does not require editing the operator's personal
    file. Neither is created implicitly.
  * **No trust on first use, ever — not even opt-in-silently.** Enrollment is a
    separate, explicit operator action: :func:`enrollment_plan` fetches the offered
    key, returns its SHA-256 fingerprint for the operator to verify out of band, and
    writes *nothing*. Persisting it requires a second call
    (:func:`enroll_verified_host`) that names the fingerprint the operator approved
    and refuses if the host now offers a different one.
  * **Never log secrets.** Nothing here logs a private key, a passphrase or a
    password. Key material is referenced by path; host keys by fingerprint.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
-----------------------------------------
It does not execute remote commands and it does not install anything on a remote
host. :func:`remote_staging_path` exists so that callers which must place a file on a
remote host get a unique, private, home-relative path instead of a shared
``/tmp/jarvis_sensor.py`` (Bandit B108) that any local user of that VM could
pre-create as a symlink or overwrite between upload and execution.
"""
from __future__ import annotations

import base64
import hashlib
import os
import posixpath
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

from core import managed_paths

#: Operator override for the JARVIS-managed ``known_hosts`` store. Read from the
#: environment only — no command, model output or remote host can redirect it.
KNOWN_HOSTS_ENV = "JARVIS_SSH_KNOWN_HOSTS"

#: Opt-in for staging files onto a remote host at all. Absent/false = plan only.
REMOTE_STAGING_ENV = "JARVIS_SENSOR_DEPLOY"

_TRUE = frozenset({"1", "true", "yes", "on"})


class HostKeyVerificationError(RuntimeError):
    """An SSH host key was unknown, mismatched, or could not be verified.

    Raised instead of connecting. Callers must surface this, not swallow it: a
    connection that fails because the host is unrecognised is a security event.
    """


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE


def trusted_lab_enabled() -> bool:
    """Mirror of the ``tools.executor`` lab gate: settings first, env as fallback."""
    try:
        from core.config import settings

        if settings.trusted_lab_mode:
            return True
    except Exception:
        pass
    return _env_true("JARVIS_TRUSTED_LAB")


def remote_staging_enabled() -> bool:
    """True only when the operator opted in AND trusted-lab mode is on.

    Both are required. Staging a file onto another machine is host mutation; it is
    not something a voice macro gets to do because an env var was left set in a
    ``.env`` from a previous engagement.
    """
    return _env_true(REMOTE_STAGING_ENV) and trusted_lab_enabled()


# ── host-key stores ─────────────────────────────────────────────────────────
def managed_known_hosts_path() -> Path:
    """The JARVIS-managed ``known_hosts`` file.

    Defaults to ``<app>/data/ssh/known_hosts`` (inside the managed, git-ignored data
    tree). ``JARVIS_SSH_KNOWN_HOSTS`` overrides it for operators who keep their lab
    host keys elsewhere. The file is NOT created here — its absence is a meaningful
    state (nothing enrolled yet), not an error to paper over.
    """
    override = os.environ.get(KNOWN_HOSTS_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return managed_paths.app_subdir("data", "ssh") / "known_hosts"


def system_known_hosts_path() -> Path:
    """The operator's own ``~/.ssh/known_hosts``."""
    return Path.home() / ".ssh" / "known_hosts"


def fingerprint(host_key) -> str:
    """``SHA256:<base64>`` — the same form OpenSSH prints, for out-of-band checks."""
    digest = hashlib.sha256(host_key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


@dataclass(frozen=True)
class HostKeyPosture:
    """Evidence of how a client was hardened. Returned so callers can log/assert it."""

    policy: str
    system_known_hosts: str | None
    managed_known_hosts: str | None
    loaded_host_count: int
    auto_add: bool = False
    stores: tuple[str, ...] = field(default_factory=tuple)


def harden_client(client) -> HostKeyPosture:
    """Apply the fail-closed host-key policy to a ``paramiko.SSHClient``.

    Must be called before ``connect``. Loads both host-key stores, then installs
    ``RejectPolicy``. Returns a :class:`HostKeyPosture` describing what was loaded.

    A missing or unreadable store is tolerated (it only means fewer hosts are
    known, which fails *more* closed, never less) but the resulting posture reports
    it honestly.
    """
    import paramiko

    stores: list[str] = []
    system_path = system_known_hosts_path()
    managed_path = managed_known_hosts_path()

    if system_path.is_file():
        try:
            client.load_system_host_keys()
            stores.append("system")
        except Exception as exc:  # unreadable/corrupt file — fail closed, not open
            logger.warning(f"SSH_POLICY: could not load system known_hosts: {exc}")

    if managed_path.is_file():
        try:
            client.load_host_keys(str(managed_path))
            stores.append("managed")
        except Exception as exc:
            logger.warning(f"SSH_POLICY: could not load managed known_hosts: {exc}")

    # Fail closed. NOT AutoAddPolicy (trust-on-first-use) and NOT WarningPolicy
    # (connects anyway and merely logs) — an unrecognised host raises.
    client.set_missing_host_key_policy(paramiko.RejectPolicy())

    try:
        loaded = len(client.get_host_keys())
    except Exception:
        loaded = 0

    posture = HostKeyPosture(
        policy="RejectPolicy",
        system_known_hosts=str(system_path) if "system" in stores else None,
        managed_known_hosts=str(managed_path) if "managed" in stores else None,
        loaded_host_count=loaded,
        auto_add=False,
        stores=tuple(stores),
    )
    if loaded == 0:
        logger.warning(
            "SSH_POLICY: no host keys are known — every SSH connection will be "
            "REJECTED until a host is explicitly enrolled. This is the safe default."
        )
    return posture


def connect_verified(client, host: str, *, username: str, key_path: str, timeout: int = 15):
    """``client.connect`` with host-key failures translated and never swallowed.

    Raises :class:`HostKeyVerificationError` for an unknown or mismatched key so the
    caller cannot mistake a rejected man-in-the-middle for an ordinary timeout.
    """
    import paramiko

    try:
        key = paramiko.RSAKey.from_private_key_file(key_path)
    except Exception as exc:
        # Deliberately reports the PATH and the exception type only — never the file
        # contents, never a passphrase.
        raise HostKeyVerificationError(
            f"SSH private key at {key_path!r} could not be loaded ({type(exc).__name__})"
        ) from exc

    try:
        client.connect(host, username=username, pkey=key, timeout=timeout)
    except paramiko.BadHostKeyException as exc:
        raise HostKeyVerificationError(
            f"SSH host key MISMATCH for {host}: offered {fingerprint(exc.key)}, "
            f"expected {fingerprint(exc.expected_key)}. Refusing to connect — this is "
            f"either key rotation or an impersonation attempt. Verify out of band."
        ) from exc
    except paramiko.SSHException as exc:
        raise HostKeyVerificationError(
            f"SSH host key for {host} is not known and JARVIS does not trust on first "
            f"use. Enroll it explicitly after verifying its fingerprint out of band "
            f"(core.ssh_policy.enrollment_plan). Underlying error: {exc}"
        ) from exc
    return client


# ── explicit, operator-approved enrollment ──────────────────────────────────
def enrollment_plan(host: str, port: int = 22, *, timeout: int = 10) -> dict:
    """Fetch the key a host offers and report its fingerprint. Writes NOTHING.

    This is the operator-facing half of enrollment: JARVIS shows what it saw, the
    operator verifies it against the console/provisioning output of the machine they
    actually own, and only then calls :func:`enroll_verified_host`. Splitting it in
    two is the point — a single "enroll this host" call would be AutoAddPolicy with
    extra steps.
    """
    import paramiko

    transport = None
    try:
        transport = paramiko.Transport((host, int(port)))
        transport.start_client(timeout=timeout)
        host_key = transport.get_remote_server_key()
    except Exception as exc:
        return {
            "host": host,
            "port": int(port),
            "error": f"could not retrieve host key ({type(exc).__name__}: {exc})",
            "enrolled": False,
        }
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass

    print_ = fingerprint(host_key)
    return {
        "host": host,
        "port": int(port),
        "key_type": host_key.get_name(),
        "fingerprint": print_,
        "enrolled": False,
        "already_known": _is_known(host, host_key),
        "operator_action": [
            f"1. Verify {print_} against the console of the machine you own.",
            "2. If and only if it matches, call core.ssh_policy.enroll_verified_host("
            f"{host!r}, {print_!r}).",
            "3. JARVIS will refuse the enrollment if the host offers a different key.",
        ],
    }


def _is_known(host: str, host_key) -> bool:
    import paramiko

    for path in (system_known_hosts_path(), managed_known_hosts_path()):
        if not path.is_file():
            continue
        try:
            entries = paramiko.HostKeys(str(path))
        except Exception:
            continue
        if entries.check(host, host_key):
            return True
    return False


def enroll_verified_host(host: str, approved_fingerprint: str, port: int = 22) -> dict:
    """Persist a host key the operator has already verified out of band.

    Refuses unless the host still offers exactly ``approved_fingerprint``, so a race
    or a redirect between verification and enrollment cannot enroll a different key.
    """
    import paramiko

    transport = None
    try:
        transport = paramiko.Transport((host, int(port)))
        transport.start_client(timeout=10)
        host_key = transport.get_remote_server_key()
    except Exception as exc:
        return {"host": host, "enrolled": False, "error": f"{type(exc).__name__}: {exc}"}
    finally:
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass

    offered = fingerprint(host_key)
    if offered != approved_fingerprint.strip():
        return {
            "host": host,
            "enrolled": False,
            "error": (
                f"fingerprint mismatch — operator approved {approved_fingerprint!r} "
                f"but the host now offers {offered!r}. Nothing was written."
            ),
        }

    store = managed_known_hosts_path()
    try:
        store.parent.mkdir(parents=True, exist_ok=True)
        entries = paramiko.HostKeys(str(store)) if store.is_file() else paramiko.HostKeys()
        entries.add(host, host_key.get_name(), host_key)
        entries.save(str(store))
        _restrict_permissions(store)
    except Exception as exc:
        return {"host": host, "enrolled": False, "error": f"{type(exc).__name__}: {exc}"}

    logger.warning(
        f"SSH_POLICY: host {host} ENROLLED by explicit operator approval "
        f"({offered}) into {store}"
    )
    return {"host": host, "enrolled": True, "fingerprint": offered, "store": str(store)}


def _restrict_permissions(path: Path) -> None:
    """Best-effort 0600 on the managed store. No-op semantics on Windows ACLs."""
    try:
        path.chmod(0o600)
    except (OSError, NotImplementedError):
        pass


# ── remote staging paths (Bandit B108) ──────────────────────────────────────
#: Permission bits for a staged remote file: owner read/write only. No execute bit —
#: nothing here starts the file, so it does not need one.
REMOTE_STAGING_MODE = 0o600


def remote_staging_path(sftp, stem: str, suffix: str = ".py") -> str:
    """A unique, private, home-relative path for a file staged on a remote host.

    Replaces the fixed ``/tmp/jarvis_sensor.py`` (Bandit B108). Three things were
    wrong with that path and all three are fixed here:

      * ``/tmp`` is world-writable and shared, so any local user on the target could
        pre-create the name as a **symlink** and redirect JARVIS's write, or swap the
        contents between upload and execution;
      * the name was **fixed**, so two deployments raced and a stale file from a
        previous engagement was silently reused;
      * it was **guessable**, so an unprivileged local process could wait for it.

    The directory is resolved from the SFTP session itself (``normalize(".")`` — the
    authenticated user's home), so no temp-directory path is hardcoded at all, and
    the leaf carries 128 bits of randomness. The name carries no secret and no
    operator-controlled text.
    """
    home = sftp.normalize(".")
    leaf = f".jarvis-{stem}-{secrets.token_hex(16)}{suffix}"
    return posixpath.join(home, leaf)
