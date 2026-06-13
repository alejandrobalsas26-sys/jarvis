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
