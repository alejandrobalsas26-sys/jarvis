# V69 M66B — Platform Capability Matrix (Phase 0)

Source master: `3bd46fbdb06744de5e07485db62050e3e9eedf6b`.
Discovery is **read-only** (§5): nothing here installs a package, edits `/etc`,
changes a sysctl or a firewall, or alters a daemon. Host configuration is
**evidence**, not something M66B changes. The measurements below were taken on
the development host on 2026-09-17 and are re-probed live by the backend at
runtime — this table is the map, `probe_capabilities()` is the territory.

## Development host

```
kernel   : Linux 7.1.5+kali-amd64 (Kali), x86_64
user     : uid=1000(kali)  non-root
python   : 3.14.7 (runtime), 3.11.16 (CI-parity gate)
```

## Linux primitive discovery

| primitive | status | evidence |
|---|---|---|
| unprivileged user namespaces | **AVAILABLE** | `kernel.unprivileged_userns_clone=1`; `user.max_user_namespaces=62225`; no `apparmor_restrict_unprivileged_userns`; `unshare -U true` → OK |
| mount namespace (unpriv) | **AVAILABLE** | `unshare -Urm` → OK |
| PID namespace (unpriv) | **AVAILABLE** | `unshare -Urpf` → OK |
| network namespace (unpriv) | **AVAILABLE** | `unshare -Urn` → OK |
| **bubblewrap** | **AVAILABLE** | `/usr/bin/bwrap`, version 0.12.0; `--unshare-all` runs unprivileged |
| cgroup v2 | AVAILABLE | `/sys/fs/cgroup` is `cgroup2fs`; delegated controllers at the user slice: `cpu memory pids` |
| `systemd-run --user --scope` | AVAILABLE | `-p MemoryMax=64M true` → OK |
| POSIX rlimits | AVAILABLE | `resource.setrlimit` for CPU/AS/FSIZE/NPROC |
| newuidmap/newgidmap + subid | AVAILABLE_BUT_UNNEEDED | `/etc/subuid`,`/etc/subgid` map `kali:100000:65536`; bwrap single-uid mode needs none of it |
| Docker (rootless) | AVAILABLE_BUT_NOT_SELECTED | `docker 29.8.1`; socket `$XDG_RUNTIME_DIR/docker.sock`, rootless. Weaker EVIDENCE than bwrap for this use (see selection) |
| Podman | NOT_AVAILABLE | not installed |
| firejail | NOT_AVAILABLE | not installed |
| runc / crun | AVAILABLE | `/usr/bin/runc` present (used indirectly by Docker) |
| slirp4netns / pasta | AVAILABLE_BUT_UNNEEDED | present; only relevant if the jail needed *filtered* networking, which SANDBOX_REQUIRED does not |
| seccomp / no_new_privs (host) | AVAILABLE | settable per-process; bwrap sets `no_new_privs` |

## Empirical backend validation (the exact production configuration)

The BubblewrapBackend configuration was validated end-to-end before any code was
written. Command shape:

```
bwrap --unshare-all --die-with-parent --new-session --clearenv --cap-drop ALL
      --ro-bind /usr /usr --symlink usr/lib /lib --symlink usr/lib64 /lib64
      --symlink usr/bin /bin --symlink usr/sbin /sbin
      --ro-bind /etc/ld.so.cache /etc/ld.so.cache
      --proc /proc --dev /dev --tmpfs /dev/shm
      --tmpfs /work --tmpfs /tmp --chdir /work
      --ro-bind <scriptdir> /jarvis_exec --uid 65534 --gid 65534
      --setenv PATH /usr/bin:/bin --setenv HOME /work
      -- python3 -I /jarvis_exec/_bootstrap.py /jarvis_exec/snippet.py
```

RLIMITs (CPU 5s, AS 512 MiB, FSIZE 16 MiB, NPROC 64) are set by `_bootstrap.py`
**inside** the jail — AFTER the UID is remapped to 65534 — then it `execv`s the
snippet. This is load-bearing: setting RLIMIT_NPROC on the bwrap *parent* counts
against the real UID's existing processes (measured: 88) and makes namespace
creation fail with EAGAIN. Inside the fresh namespace UID the count starts at 0.

| mandatory control (§11) | measured result |
|---|---|
| filesystem isolation | `/home/kali` absent, repo absent, `/run`,`/var` absent inside jail |
| host home canary | unreadable (path does not exist in jail) |
| network default-deny | `connect(("127.0.0.1", <host port>))` → ConnectionRefused |
| host loopback isolation | live host listener received **0** connections from the jail |
| runtime socket | `/run/docker.sock`, `/var/run/docker.sock` absent inside jail |
| descendant containment (PID ns) | 3 members (parent→child→setsid-grandchild) all reaped when the jail is torn down; 0 survivors |
| setsid escape | BLOCKED (the setsid grandchild is inside the PID ns) |
| PID limit | RLIMIT_NPROC=64 burst of 120 `sleep` → 62 spawned, 58 EAGAIN |
| CPU limit | RLIMIT_CPU=5s enforced |
| memory limit | RLIMIT_AS=512 MiB (`getrlimit` confirms inside jail) |
| storage limit | 200 MiB write into 64 MiB tmpfs → OSError (ENOSPC) |
| output limit | stdout/stderr capped by the backend |
| environment isolation | `JARVIS_M66B_SECRET_CANARY` → `None` inside jail; benign `PATH` present |
| privilege | uid/euid 65534 (non-root); `CapEff=0`; `NoNewPrivs=1` |
| workspace ephemeral | fresh `tmpfs /work` per run; torn down on exit |

Every mandatory control was observed ENFORCED. bubblewrap therefore yields an
evidence-derived **SANDBOXED** profile on this host.

## Selection rationale (feeds §6 of the plan)

Preference order requested: *non-privileged OS sandbox primitive > rootless OCI >
trusted-daemon OCI > RESTRICTED_PROCESS.* bubblewrap is the top preference AND
produces the strongest evidence here:

* It is **non-privileged** (setuid helper, no daemon, no root).
* Every isolation property is **directly observable** in the child's `/proc`
  and by causal probes — no daemon indirection to reason about.
* Rootless Docker is available but adds a **daemon to the TCB** and produces
  *weaker, less direct* evidence for this narrow "run one Python snippet with no
  network" job; §6 forbids adopting a more complex backend that yields weaker
  evidence. Docker is therefore documented as available and NOT selected.

## CI / portability reality

GitHub `ubuntu-latest` runners do **not** ship `bwrap` and the CI gates install
nothing extra. There, `probe_capabilities()` reports the sandbox UNAVAILABLE, and
`code_execute` (default SANDBOX_REQUIRED) **fails closed** unless the operator has
set `JARVIS_EXEC_CONTAINMENT=compat`. Live-sandbox tests are `skipif`-gated on
`bwrap` + working userns, so they run here and skip in CI; the derivation,
fail-closed, no-downgrade, and receipt-truth tests are pure logic and run
everywhere.

## Windows

Audited from documentation only (this is a Linux host): Job Objects
(process/CPU/memory limits, kill-on-close), restricted tokens, low-integrity,
AppContainer, process-mitigation policies. None are live-verifiable here, so the
WindowsContainmentBackend is implemented truthfully as
`WINDOWS_RESTRICTED_PROCESS_ONLY` and never claims SANDBOXED (§25).
