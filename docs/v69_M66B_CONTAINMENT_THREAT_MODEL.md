# V69 M66B — Execution-Isolation & Network-Containment Threat Model (Phase 0)

Source master: `3bd46fbdb06744de5e07485db62050e3e9eedf6b`
Produced from repository reality at branch creation. NO production code was
edited before this document, the platform matrix and the execution-surface map
existed (§4).

M66A established epistemic/verification truth. M66A.1 established the four-layer
security model and made `code_execute` a truthful **RESTRICTED_PROCESS**. M66B
exists to materially strengthen **L3 — Execution Containment** for arbitrary code
execution, and to prove the strengthening with evidence rather than a rename.

The four layers, unchanged:

* **L1 Decision & Authority** — SHOULD this run? (`authorize_action`,
  `classify_tool`/`RiskClass`, `_ALWAYS_HITL_TOOLS`, HITL `_challenge`.)
* **L2 Resource Boundaries** — WHAT may it touch? (`_resolve_within_allowed`,
  `_http_target_blocked`.)
* **L3 Execution Containment** — how much power does execution REALLY have?
  **This is M66B's target.**
* **L4 Truth / Audit / Recovery** — what actually happened? (effect journal,
  containment receipt, status enums.)

## 1. Absolute principles carried into every decision below

```
AUTHORIZATION != CONTAINMENT          SUBPROCESS != SANDBOX
CONTAINER    != AUTOMATICALLY SANDBOX SANDBOXED IS EVIDENCE-DERIVED
NO SILENT FALLBACK                    NO MODEL-CONTROLLED SECURITY DOWNGRADE
FAILURE TO ESTABLISH REQUIRED CONTAINMENT = DO NOT EXECUTE
```

M66B must not merely rename RESTRICTED_PROCESS to SANDBOXED.

## 2. The asset and the adversary

**Asset:** the host JARVIS runs on — its filesystem (operator home, the JARVIS
repository, SSH keys), its network position (loopback services, LAN, cloud
metadata), its process table, its environment (API keys, tokens), and its
privilege (the operator's UID).

**Adversary:** the *code being executed*. `code_execute` runs a Python snippet
that reaches the handler through the L1/HITL pipeline — the snippet's TEXT is
authored by the model or, downstream, by untrusted content the model relayed. An
approved call to `code_execute` is authorization to run *a* snippet; it is NOT a
statement that the snippet is trustworthy. The snippet is the thing we contain.

**Out of trust:** the snippet, its descendants, any value it can read from the
environment or filesystem, any socket it can open.

**In the trusted computing base (TCB):** the Python interpreter, the host kernel
and its unprivileged-namespace machinery, `bwrap` (setuid helper installed by the
distribution), and the JARVIS broker that CONFIGURES the sandbox. The broker is
trusted to *build* the jail; the code inside it is not trusted with anything.

## 3. What M66A.1 left open (the limitations M66B attacks)

Recorded in M66A.1's `ContainmentReport` and security surface map:

| # | M66A.1 limitation | M66B disposition |
|---|---|---|
| T1 | network isolation NOT_ENFORCED — a snippet can open outbound sockets | CLOSE via network namespace (`--unshare-net`) |
| T2 | no strong host-filesystem isolation — snippet sees the whole host FS | CLOSE via mount namespace (only `/usr` ro + ephemeral tmpfs) |
| T3 | process-group cleanup escapable by a descendant that calls `setsid()` | CLOSE via PID namespace (killing the jail reaps the whole ns) |
| T4 | no containment-scoped PID limit (RLIMIT_NPROC left per-UID) | CLOSE via RLIMIT_NPROC set INSIDE the remapped-UID namespace |
| T5 | Windows has no demonstrated strong-containment parity | TRUTHFUL: WindowsContainmentBackend audited, not live-verified |
| T6 | L2 HTTP DNS validation does not claim rebinding resistance | RE-AUDITED; independent of L3 (a strong sandbox has no network) |

## 4. Attack surfaces and the containment story for each

Every attack the escape matrix (§34) exercises maps to a namespace or a limit:

* **Read a host secret file** (`~/.ssh/id_rsa`, an env-derived token file):
  mount namespace — the path does not exist inside the jail.
* **Reach a loopback service** (a dev database on 127.0.0.1, `::1`): network
  namespace — the jail has only a down `lo`, no route to the host stack.
* **Reach cloud metadata** (169.254.169.254) or the LAN: network namespace — no
  interface is up, nothing is routable.
* **Read an inherited API key** from `os.environ`: `--clearenv` + an explicit
  allowlist — the child starts with PATH/HOME only.
* **Spawn a `setsid` grandchild that survives the timeout**: PID namespace —
  the grandchild is inside the ns; tearing the jail down kills init(pid 1) and
  the kernel reaps every member.
* **Fork-bomb / exhaust PIDs**: RLIMIT_NPROC, now effective because the jail's
  UID is remapped to a fresh namespace UID with zero existing processes.
* **Burn CPU / exhaust memory / fill the disk / flood stdout**: RLIMIT_CPU,
  RLIMIT_AS, a size-bounded tmpfs + RLIMIT_FSIZE, output caps.
* **Talk to the container runtime** to escape (`/run/docker.sock`): the socket
  is never mounted; the jail cannot see it.
* **Escalate privilege** (`setuid`, ptrace a host process): non-root UID, all
  capabilities dropped, `no_new_privs`, and a separate user namespace.

## 5. Trust-boundary rules (non-negotiable)

1. **The broker receives an ALREADY-AUTHORIZED operation.** It never grants
   authority, never widens `AuthorizedSecurityScope`, never raises autonomy,
   never bypasses HITL. It only decides *how strongly to contain* and *whether
   containment can be established at all*. If it cannot, it refuses execution.
2. **Content is not authority (§8).** `sandbox=false`, `privileged=true`,
   `host_network=true`, `allow_host_fs=true`, `disable_network_isolation=true`,
   `use_direct_process=true`, `bypass_containment=true` in `tool_input` MUST NOT
   change trusted containment policy. They are stripped/ignored and tested with
   hostile-input cases.
3. **No silent fallback.** SANDBOX_REQUIRED that cannot be satisfied fails
   closed by default. A weaker backend NEVER satisfies a stronger requirement.
4. **The profile is DERIVED, never asserted (§13).** `SANDBOXED` is emitted only
   when every mandatory control (§11) is observed ENFORCED. Any missing mandatory
   control ⇒ the derived profile is not SANDBOXED, whatever the backend intended.
5. **Operator compatibility is explicit, host-controlled and visible.** The only
   sanctioned way to run arbitrary code without a sandbox is an operator-set
   environment policy (`JARVIS_EXEC_CONTAINMENT=compat`); it is deterministic,
   auditable, reported as RESTRICTED_PROCESS with a `downgraded` marker, and
   NEVER reported SANDBOXED. It is not model-controlled and defaults to strict.

## 6. Residual risks M66B does NOT claim to close

* **Kernel / bwrap 0-day.** The jail rests on the host kernel's namespace
  implementation and the setuid `bwrap` helper. A kernel LPE or a bwrap bug is
  outside this milestone; both are in the TCB.
* **Side channels** (timing, cache, `/proc` info leaks about the host kernel).
  Not addressed; a snippet can still learn the kernel version.
* **DNS rebinding on the L2 HTTP path (T6).** Re-audited in §33 of the plan and
  left truthfully `NOT_ENFORCED`; it does not affect L3 because a SANDBOXED
  execution has no network at all.
* **Windows strong containment.** Audited (Job Objects, restricted tokens,
  AppContainer) but not live-verified on this Linux host; reported as such, never
  as parity.

No implementation precedes this document.
