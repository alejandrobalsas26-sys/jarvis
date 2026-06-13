# Changelog

## V60.0 — Hardening, role routing, and installability

### Security (Phase 7)
- **Neutralized the `FORCE_OVERRIDE` guardrail bypass.** LLM-generated tool
  arguments can no longer disable destructive-pattern guardrails. The key is
  stripped and logged at both execution gates; the only legitimate override is
  operator-set trusted-lab mode (`JARVIS_TRUSTED_LAB`), read from `.env`/env.
- **SSRF defense for `http_request`.** Loopback, RFC1918 private, link-local
  (incl. `169.254.169.254` cloud metadata), multicast, and reserved targets are
  blocked — including hostnames that resolve to them — unless trusted-lab mode
  is enabled.
- Added `core/config.py: trusted_lab_mode` (env-only, hardened off by default).

### Architecture
- **Role-based model router** (`core/model_router.py`): `ModelRole`,
  `ModelDecision`, `route()` with bilingual (EN/ES) keyword scoring and
  env-overridable, hardware-friendly defaults. Legacy `select_model` /
  `calculate_complexity` remain backward compatible.
- **Planner/verifier** (`core/verification.py`): `should_verify` and a
  fail-closed `verify_answer` that audits drafts with a dedicated VERIFIER model.
- **Memory discipline** (`core/memory_router.py`): secret refusal, scope
  classification, and untrusted-source tagging.
- **Hardware-tier model profiles** (`core/hardware_model_profile.py`): LOW/MID/
  HIGH/EXTREME tiers → recommended models and `ollama pull` commands.

### Bug fixes
- Fixed a **Python 3.11 SyntaxError** in `core/session_journal.py` (backslash
  escape inside an f-string expression — a 3.12+ feature).
- Fixed a latent **`NameError`** in `main.py`'s voice path: `_process_voice_input`
  used `is_interrupt_command` / `handle_interrupt` / `process_for_macro` without
  importing them in scope.

### Lint cleanup (Phase 12 follow-through)
- Ruff gate expanded to **`E9` + full pyflakes (`F`)** and the tree made clean:
  81 unused imports / empty f-strings auto-fixed, 3 unused variables removed, one
  genuinely-unused name dropped from a multi-import.
- **Avoided two autofix regressions** (verified, not blindly applied):
  - `core/self_test.py` uses `try: import <dep>` as availability probes — the
    bound name is intentionally unused. Reverted the removals and added a
    documented per-file `F401` ignore (proven false positive).
  - `main.py` early `.env` validation (`from core.config import settings`) is a
    side-effect import; restored with a narrow commented `# noqa: F401`.
- `core/sensor_agent_template.py` keeps its documented `F821` per-file ignore
  (`__JARVIS_PORT__` is string-substituted at runtime).

### Documentation drift fixed
- Root `README.md` (new) and `jarvis/README.md` (rewritten): corrected the brain
  from "Claude Sonnet" to **Ollama (local default)**; `ANTHROPIC_API_KEY` is
  documented as optional/cloud-only. Removed the stale "migrate to Ollama"
  roadmap item (already done).
- Added `docs/TROUBLESHOOTING.md`.

### Installability & tooling (Phases 1, 2, 9, 10, 11)
- `requirements/` profiles: `base`, `voice`, `docs`, `soc`, `lab`, `dev`, `all`.
  Base is lean enough for text mode without audio/OCR/ML/lab deps.
- `scripts/install.ps1`, `scripts/install.sh`, `scripts/doctor.py`,
  `scripts/model_doctor.py`.
- `pyproject.toml`: metadata, `requires-python >= 3.11`, optional-dependency
  groups, ruff config, pytest config + markers, `jarvis` console script.
- `python -m jarvis` entrypoint (`python main.py` still works).
- CI split into `lint` (ruff gate, **no `|| true`**), `tests-base`, `security`
  (bandit + pip-audit), and `docker-build`.
- Dockerfile builds the lean base image (the old monolithic image pinned the
  Windows-only `torch-directml` wheel and could not build on Linux).
- Docs: `SECURITY.md`, `docs/THREAT_MODEL.md`, this changelog.

### Tests
- New: `tests/test_security_hardening.py` (FORCE_OVERRIDE + SSRF),
  `jarvis/tests/test_model_router_roles.py`,
  `jarvis/tests/test_memory_and_verification.py`,
  `jarvis/tests/test_hardware_model_profile.py`.

### Roadmap / follow-ups
- Ruff is gated on correctness rules (`E9, F63, F7, F82`); a repo-wide
  unused-import (`F401`) and style cleanup is the next lint PR.
- Verifier integration is wired as a standalone module; hooking it into the
  streaming response path (non-streaming/security-sensitive paths first) is the
  next orchestration PR.
