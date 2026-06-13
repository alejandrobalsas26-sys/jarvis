# JARVIS Threat Model

A focused threat model for the parts of JARVIS that take untrusted input: the
LLM-driven tool executor, outbound network tools, file I/O, and memory.

## Assets

- The operator's host (files, OS integrity, credentials).
- The local network / lab and any reachable production systems.
- Conversation history and persisted memory.
- The audit trail (`tactic_audit.jsonl`, session journals).

## Trust boundaries

| Source | Trust | Notes |
|---|---|---|
| Operator (keyboard / NATO voice) | Trusted | Final authority for gated tools |
| Local Ollama model output | **Untrusted** | May hallucinate tools, emit injection payloads |
| Web / file / RAG / tool results | **Untrusted** | Tagged untrusted for memory writes |
| `.env` / environment | Trusted (operator) | Sole source of `JARVIS_TRUSTED_LAB` |
| Cloud backend (if enabled) | Untrusted egress | Off by default |

## Threats and mitigations

### T1 — LLM disables a guardrail via tool arguments
A model emits `FORCE_OVERRIDE=true` (or similar) to bypass destructive-pattern
blocks. **Mitigated (V60.0):** the executor strips `FORCE_OVERRIDE` from every
tool input before validation and logs it; the only override is operator-set
`JARVIS_TRUSTED_LAB`, read from the environment only.

### T2 — SSRF / cloud-metadata theft via `http_request`
A model requests `http://169.254.169.254/...` or a hostname that resolves to an
internal IP. **Mitigated (V60.0):** outbound HTTP resolves the host and rejects
loopback / private / link-local / multicast / reserved addresses (all resolved
IPs), unless trusted-lab mode is on.

### T3 — Command injection through `run_shell_command`
A model injects shell metacharacters or encoded commands. **Mitigated:**
allowlist of binaries, `shell=False`, forbidden-metacharacter regex, blocked
`-EncodedCommand` / `python -c`, path canonicalization, and a static
triage/neutralization pipeline for blocked commands.

### T4 — Path traversal / system file access
**Mitigated:** `read_file` / `write_file` are sandboxed to Downloads, Documents,
and the project dir; system directories are blocked by canonicalization.

### T5 — Secret exfiltration into memory or logs
**Mitigated:** `core/memory_router.contains_secret` refuses to persist API keys,
tokens, passwords, cookies, and private keys; tool outputs are scanned for PII
and redacted before entering prompt history.

### T6 — Hallucinated / unsafe answers acted on without review
**Mitigated:** `core/verification.should_verify` flags security-sensitive,
tool-using, and deep-analysis turns for a separate VERIFIER-model pass that
fails closed (treats verifier outages as "needs human review").

### T7 — Unauthorized use of lab-only offensive modules
**Mitigated:** offensive-capable tooling lives in the `lab` profile, is not
installed by `base`, and is gated by HITL/NATO approval at runtime. Operator
intent is required; nothing fires autonomously.

## Residual risks

- Trusted-lab mode intentionally relaxes T1/T2 — only enable it on an isolated
  network.
- The verifier and guardrails reduce but do not eliminate the risk of a
  convincing-but-wrong answer; the operator remains the final reviewer.
- A fully minimal `base` install may not boot every eagerly-imported subsystem;
  text mode is the supported base surface (see CHANGELOG roadmap).
