# Security Policy

JARVIS is a **local-first, defensive-by-default** AI workstation for an
authorized homelab. It ships offensive-capable, lab-only modules that are gated
behind explicit human approval and an opt-in trusted-lab flag. This document
describes the security model and how to report issues.

## Scope and intent

- **Authorized use only.** JARVIS is for the operator's own systems, an isolated
  lab, CTFs, and defensive security work. Lab-only / offensive-capable modules
  (MITM proxy, C2 bridges, Metasploit RPC, RF tooling) are gated and must never
  be pointed at third-party systems without authorization.
- **Local-first.** The default LLM backend is **Ollama** running on localhost.
  No prompt data leaves the machine unless you explicitly enable the cloud
  backend (`JARVIS_CLOUD_ENABLED=true`).

## Tool authorization model

Every model-invoked tool passes through `tools/executor.py`, which enforces, in
order:

1. **Override stripping** — any `FORCE_OVERRIDE` key in an LLM-generated tool
   argument is removed and logged. The LLM cannot disable guardrails. *(V60.0)*
2. **Pre-flight validation** — per-tool input checks (network targets, domains,
   scan flags) reject shell metacharacters and injection payloads.
3. **Destructive-pattern guardrails** — root deletes and writes to
   `C:\Windows` / `System32` are blocked. The only override is operator-set
   **trusted-lab mode** (`JARVIS_TRUSTED_LAB=true`, read from `.env`/env only).
4. **Command allowlist + `shell=False`** — `run_shell_command` accepts only
   allowlisted binaries, blocks `-EncodedCommand` / `python -c`, and
   canonicalizes paths away from system directories.
5. **HITL / NATO vocal MFA** — non-exempt tools require an interactive
   human-in-the-loop challenge before they run.
6. **SSRF defense** — `http_request` rejects loopback, RFC1918 private,
   link-local (incl. `169.254.169.254` cloud metadata), multicast, and reserved
   targets — including hostnames that resolve to them — unless trusted-lab mode
   is enabled. *(V60.0)*
7. **Sandboxed file I/O** — `read_file` / `write_file` are confined to
   Downloads, Documents, and the project directory.
8. **Audit logging + PII detection** on every tool result.

## Declared postures (V69 M61)

Four capabilities were removed or gated in M61.7. They are declared here rather than
merely absent from the code, because "we quietly stopped doing it" is not a posture an
operator can rely on. `core.release_check.check_security_posture()` fails the build if
any current-facing document advertises one of them as active, and if this section stops
declaring them.

- **Dynamic source plugins:** *refused, fail-closed.* `core/plugin_loader.py` no longer
  executes plugin source. There is **no plugin sandbox** — the previous one was an
  `exec` namespace, which restricts names and not privileges, and was demonstrably
  escapable. There is deliberately **no environment variable to re-enable execution**:
  an opt-in flag would be the same vulnerability behind a different default. Manifest
  parsing, integrity verification and reporting still work; refusals stay visible via
  `REFUSED_PLUGINS` and `status()["dynamic_exec_supported"]`, so a dashboard cannot show
  "0 plugins" and leave you believing the directory was empty.
- **SSH host keys:** *verified, unknown keys refused.* `core/ssh_policy.py` uses
  `RejectPolicy`, never `AutoAddPolicy`. Enrollment is an explicit two-step operator
  action; there is no trust-on-first-use. Automated remote sensor deployment is **off by
  default** and returns an action plan instead; the opt-in stages a file to a unique,
  private, home-relative path and never installs or starts anything remotely.
- **Service network exposure:** *loopback-first, explicit opt-in.* The decoy, tarpit,
  active-tarpit, DNS-sinkhole and canary services bind loopback unless the operator sets
  the service's `JARVIS_<SERVICE>_EXPOSE` variable, and every exposure logs a WARNING
  naming the service and the proven bind address. An operator-named single address is
  honoured as the narrower option. The safe default logs nothing, so the warning means
  something.
- **Dependency installation:** *report-only by default.* The boot-time dependency
  guardian reports what is missing and installs nothing unless the operator sets
  `JARVIS_AUTO_INSTALL_DEPS=true`. `--break-system-packages` is not passed at all: it
  exists to override the guard protecting an externally-managed interpreter, and if that
  guard fires, refusing is the correct outcome.

## Trusted-lab mode

`JARVIS_TRUSTED_LAB=true` relaxes (4-tier) controls for an **isolated, authorized
lab**: it permits the destructive-pattern override and internal-range HTTP. It is
read exclusively from the environment / `.env`, never from a model or tool
argument. Leave it **off** on any machine with reachable production networks.

## Reporting a vulnerability

This is a personal project. Report issues privately to the maintainer
(`alejandrobalsas26@gmail.com`). Please include reproduction steps and the
affected module. Do not open public issues for exploitable findings.

See [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md) for the full threat model.
