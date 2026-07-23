"""scripts/qualify_runtime_m59.py — V69 M59.3: bounded prefix qualification harness.

A reusable, BOUNDED replacement for the M58 manual benchmark. It runs the deterministic
prefix/runner-identity matrix (always, no server) and — only with ``--live`` and a
reachable loopback Ollama — a BOUNDED number of real generations judged against a
power-appropriate threshold profile. It emits a machine-readable, content-safe JSON
artifact and a concise human report.

SAFETY POSTURE — what this harness will NEVER do
------------------------------------------------
  * no model pulled/downloaded; no Ollama setting written; no restart;
  * no semantic collection read/written; no tool run; no git mutation;
  * no environment or host-configuration change;
  * only loopback Ollama is contacted, and only under ``--live``;
  * the artifact carries fixture IDs, fingerprints, counts and milliseconds — never a
    raw prompt, a generated body, a secret, or a private path.

Usage (from the repo root)::

    python jarvis/scripts/qualify_runtime_m59.py            # deterministic only
    python jarvis/scripts/qualify_runtime_m59.py --quick --live
    python jarvis/scripts/qualify_runtime_m59.py --full --live --json --output q.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.qualification import (  # noqa: E402
    FIXTURES,
    FULL_LIVE_FIXTURES,
    QUICK_LIVE_FIXTURES,
    build_artifact,
    evaluate_live_case,
    host_profile_snapshot,
    run_deterministic_matrix,
    select_threshold_profile,
)


def _git_metadata() -> dict:
    """Read-only git commit/branch. Never mutates. Failure is captured, not fatal."""
    def _read(args):
        try:
            out = subprocess.run(["git", *args], capture_output=True, text=True,
                                 timeout=5.0)
            return out.stdout.strip() if out.returncode == 0 else None
        except Exception:  # noqa: BLE001
            return None
    return {"commit": _read(["rev-parse", "HEAD"]),
            "branch": _read(["rev-parse", "--abbrev-ref", "HEAD"])}


async def _observed_residency() -> list:
    try:
        import httpx
        from core.ollama_native import default_base_url
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(default_base_url().rstrip("/") + "/api/ps")
            if r.status_code == 200:
                return [m.get("name", "") for m in (r.json() or {}).get("models", [])
                        if m.get("name")]
    except Exception:  # noqa: BLE001
        pass
    return []


async def _ollama_version() -> str | None:
    try:
        import httpx
        from core.ollama_native import default_base_url
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(default_base_url().rstrip("/") + "/api/version")
            if r.status_code == 200:
                return str((r.json() or {}).get("version") or "")
    except Exception:  # noqa: BLE001
        return None
    return None


def _power_profile() -> str:
    try:
        from core.runtime_profile import PowerSource, psutil_power_reader
        src, _ = psutil_power_reader()
        return src.value if isinstance(src, PowerSource) else str(src)
    except Exception:  # noqa: BLE001
        return "UNKNOWN"


def _model_roles() -> dict:
    roles = {}
    try:
        from core.model_router import ModelRole, model_for_role
        for role in ModelRole:
            try:
                roles[role.value] = model_for_role(role) or ""
            except Exception:  # noqa: BLE001
                roles[role.value] = ""
    except Exception:  # noqa: BLE001
        pass
    return roles


async def _live_measure(fixture) -> dict:
    """Run ONE bounded live generation for a fixture and return a content-free metrics
    dict, or {"error": reason} when the server is unreachable."""
    from core import host_time as _ht
    from core.config import settings
    from core.generation_budget import budget_for_shape
    from core.language_context import LanguageContext
    from core.model_router import ModelRole, model_for_role
    from core.ollama_native import CancellationToken, chat_stream
    from core.prefix_cache import get_prefix_cache_observer
    from core.prompt_manifest import build_fast_system_prompt, build_manifest
    from core.response_contract import select_contract
    from core.tool_schema import EMPTY_TOOL_SCHEMA_FINGERPRINT
    from core.turn_budget import StageTimeouts, TurnBudget
    from core.turn_policy import classify_request

    d0 = time.monotonic()
    lang_ctx = LanguageContext()
    try:
        lang_ctx.observe_text(fixture.prompt)
    except Exception:  # noqa: BLE001
        pass
    policy = classify_request(fixture.prompt)
    model = (getattr(settings, "fast_model", "") or "").strip() \
        or model_for_role(ModelRole.FAST) or ""
    think = settings.fast_think_value() if hasattr(settings, "fast_think_value") else False
    lang = lang_ctx.active_language() or fixture.language
    shape = select_contract(fixture.prompt, turn_policy=policy, language=lang)
    gen = budget_for_shape(shape, settings=settings)
    try:
        lang_dir = lang_ctx.directive()
    except Exception:  # noqa: BLE001
        lang_dir = ""
    system = build_fast_system_prompt(language_directive=lang_dir, shape=shape,
                                      host_time_line=_ht.host_time_prompt_line(),
                                      continuation="")
    manifest = build_manifest(model=model, transport="native", think=think,
                              num_ctx=int(gen.num_ctx), language=lang,
                              language_directive=lang_dir, authority_mode="STANDARD",
                              scope_fingerprint="",
                              tool_schema_fingerprint=EMPTY_TOOL_SCHEMA_FINGERPRINT,
                              shape=shape)
    dispatch_ms = round((time.monotonic() - d0) * 1000.0, 1)
    budget = TurnBudget(total_s=float(gen.total_turn_s or 60.0))
    timeouts = StageTimeouts(connect_s=5.0, first_token_s=float(gen.first_token_s or 90.0),
                             idle_s=float(gen.idle_s or 20.0), total_s=budget.total_s)
    t0 = time.monotonic()
    first_ms = pe_count = pe_ms = load_ms = None
    try:
        async for ch in chat_stream(
                model=model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": fixture.prompt}],
                think=think, max_tokens=int(gen.num_predict),
                temperature=float(gen.temperature),
                options_extra=gen.options(), budget=budget, timeouts=timeouts,
                ctx=int(gen.num_ctx), keep_alive=gen.keep_alive,
                cancellation=CancellationToken.from_event(None)):
            if ch.content and first_ms is None:
                first_ms = round((time.monotonic() - t0) * 1000.0, 1)
            if ch.done:
                pe_count = ch.prompt_eval_count
                pe_ms = round(ch.prompt_eval_duration / 1e6, 1) if ch.prompt_eval_duration else None
                load_ms = round((ch.load_duration or 0) / 1e6, 1)
                break
    except Exception as exc:  # noqa: BLE001
        return {"error": type(exc).__name__}
    total_ms = round((time.monotonic() - t0) * 1000.0, 1)
    state = get_prefix_cache_observer().classify(
        compatibility_identity=manifest.compatibility_identity(),
        prompt_eval_count=pe_count, prompt_eval_ms=pe_ms, load_ms=load_ms,
        first_content_ms=first_ms)
    return {"dispatch_ms": dispatch_ms, "prompt_eval_ms": pe_ms,
            "first_content_ms": first_ms, "total_ms": total_ms, "load_ms": load_ms,
            "cache_state": state.value, "num_ctx": int(gen.num_ctx)}


async def _run_live(fixture_ids) -> list:
    """Bounded live cases: warm CONCISE once, then measure each fixture (a repeat of
    the first proves reuse). Never exceeds the documented fixture set."""
    cases = []
    warm = True
    power = _power_profile()
    try:
        from core.contract_family import ContractFamily, get_family_prewarm
        await get_family_prewarm().warm_family(ContractFamily.CONCISE)
    except Exception:  # noqa: BLE001
        warm = False
    for fid in fixture_ids:
        fixture = FIXTURES[fid]
        metrics = await _live_measure(fixture)
        profile = select_threshold_profile(power_profile=power,
                                           warm=warm and not (metrics.get("load_ms") or 0) > 800)
        cases.append(evaluate_live_case(f"live_{fid}", metrics, profile))
    return cases


async def _amain(args) -> dict:
    cases = list(run_deterministic_matrix())
    warnings = []
    live_requested = bool(args.live)
    thresholds = None
    ollama_version = None
    residency = []
    if live_requested:
        fixture_ids = QUICK_LIVE_FIXTURES if args.quick else FULL_LIVE_FIXTURES
        ollama_version = await _ollama_version()
        residency = await _observed_residency()
        if ollama_version is None:
            warnings.append("ollama_unreachable")
        cases += await _run_live(fixture_ids)
        thresholds = select_threshold_profile(power_profile=_power_profile(), warm=True)
    else:
        warnings.append("live_skipped_deterministic_only")
    return build_artifact(
        cases, mode=("quick" if args.quick else "full"),
        live_requested=live_requested, timestamp=time.time(),
        git=_git_metadata(), host=host_profile_snapshot(),
        power_profile=_power_profile(), ollama_version=ollama_version,
        model_roles=_model_roles(), observed_residency=residency,
        thresholds=thresholds, warnings=warnings)


def _print_report(art: dict) -> None:
    print("=" * 78)
    print("JARVIS V69 M59.3 - runtime prefix qualification")
    print("=" * 78)
    print(f"mode={art['mode']} verdict={art['verdict']} power={art['power_profile']}")
    print(f"git={art['git'].get('commit', '?')[:12] if art['git'].get('commit') else '?'} "
          f"branch={art['git'].get('branch')}")
    c = art["counts"]
    print(f"cases: pass={c['passed']} fail={c['failed']} "
          f"insufficient={c['insufficient_evidence']} degraded={c['degraded']}")
    for case in art["cases"]:
        print(f"  [{case['verdict']:<22}] {case['case_id']:<32} {case['detail']}")
    if art["warnings"]:
        print(f"warnings: {art['warnings']}")
    print("=" * 78)


def main() -> None:
    ap = argparse.ArgumentParser(description="M59.3 bounded prefix qualification")
    ap.add_argument("--quick", action="store_true", help="the quick bounded matrix")
    ap.add_argument("--full", action="store_true", help="the full bounded matrix")
    ap.add_argument("--live", action="store_true", help="run bounded live generations")
    ap.add_argument("--no-live", action="store_true", help="deterministic only (default)")
    ap.add_argument("--json", action="store_true", help="print the JSON artifact")
    ap.add_argument("--output", metavar="PATH", help="write the JSON artifact to PATH")
    args = ap.parse_args()
    if args.no_live:
        args.live = False
    art = asyncio.run(_amain(args))
    if args.json:
        print(json.dumps(art, indent=2))
    else:
        _print_report(art)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            json.dump(art, fh, indent=2)
    # Exit non-zero on a hard FAIL so CI can gate on it; INSUFFICIENT_EVIDENCE is not a
    # hard failure (a missing server is not a code regression).
    sys.exit(1 if art["verdict"] == "FAIL" else 0)


if __name__ == "__main__":
    main()
