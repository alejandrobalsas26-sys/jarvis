"""scripts/bench_prefix_m58.py — V69 M58.9: bounded live prefix-parity benchmark.

Measures PROMPT-PREFIX PARITY against the real host, through the real seams: the
stable-prefix builder (core.prompt_manifest), the compact contract delta, the native
Ollama transport, and the prefix-cache observer. It reasons ONLY from observable
evidence (prompt_eval_count / prompt_eval_duration / load_duration / first content),
and classifies reuse HONESTLY — model residency alone is never called reuse.

SAFETY POSTURE — what this script will never do
-----------------------------------------------
  * no model is pulled or downloaded; no Ollama setting is written; no restart;
  * no semantic collection is read/written/reindexed;
  * no tool runs; no filesystem path is opened; only loopback Ollama is contacted;
  * nothing is written to conversation history, runtime health or semantic memory.

Usage (from the repo root)::

    python jarvis/scripts/bench_prefix_m58.py
    python jarvis/scripts/bench_prefix_m58.py --quick
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The M58 live prefix sequence: same-family repeats prove stable-prefix reuse; a
# family switch proves the compact delta does NOT re-pay the M57 prose penalty.
PROMPTS: list[tuple[str, str]] = [
    ("hola", "hola"),
    ("instant-2", "buenas"),
    ("brief-sqrt", "como saco la raiz cuadrada de algo"),
    ("brief-2", "y la raiz cubica?"),
    ("standard-poo", "explicame POO brevemente"),
    ("technical", "explica herencia en Python con un ejemplo"),
]
QUICK = {"hola", "instant-2", "brief-sqrt"}


async def _residency() -> list[str]:
    """Best-effort observed model residency (loopback /api/ps). Never fatal."""
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


async def _measure(label: str, prompt: str) -> dict:
    from core import host_time as _ht
    from core.config import settings
    from core.generation_budget import budget_for_shape
    from core.language_context import LanguageContext
    from core.model_router import ModelRole, model_for_role
    from core.ollama_native import CancellationToken, chat_stream
    from core.prefix_cache import get_prefix_cache_observer
    from core.prompt_manifest import (
        build_fast_system_prompt, build_manifest, contract_delta,
    )
    from core.response_contract import select_contract
    from core.tool_schema import EMPTY_TOOL_SCHEMA_FINGERPRINT
    from core.turn_budget import StageTimeouts, TurnBudget
    from core.turn_policy import classify_request

    lang_ctx = LanguageContext()
    try:
        lang_ctx.observe_text(prompt)
    except Exception:  # noqa: BLE001
        pass
    policy = classify_request(prompt)
    _model = (getattr(settings, "fast_model", "") or "").strip() \
        or model_for_role(ModelRole.FAST) or ""
    _think = settings.fast_think_value() if hasattr(settings, "fast_think_value") else False

    class _Route:  # minimal route view for the fields the bench reads
        model = _model
        think = _think
    route = _Route()
    shape = select_contract(prompt, turn_policy=policy,
                            language=lang_ctx.active_language() or "es")
    gen = budget_for_shape(shape, settings=settings)
    lang_dir = ""
    try:
        lang_dir = lang_ctx.directive()
    except Exception:  # noqa: BLE001
        lang_dir = ""
    system = build_fast_system_prompt(
        language_directive=lang_dir, shape=shape,
        host_time_line=_ht.host_time_prompt_line(), continuation="")
    manifest = build_manifest(
        model=route.model, transport="native", think=route.think,
        num_ctx=int(gen.num_ctx), language=lang_ctx.active_language() or "es",
        language_directive=lang_dir, authority_mode="STANDARD",
        scope_fingerprint="", tool_schema_fingerprint=EMPTY_TOOL_SCHEMA_FINGERPRINT,
        shape=shape)
    delta = contract_delta(shape, language=lang_ctx.active_language() or "es")

    budget = TurnBudget(total_s=float(gen.total_turn_s or 60.0))
    timeouts = StageTimeouts(connect_s=5.0, first_token_s=float(gen.first_token_s or 90.0),
                             idle_s=float(gen.idle_s or 20.0), total_s=budget.total_s)
    row = {"label": label, "contract": shape.contract.value,
           "stable_fp": manifest.stable_prefix_fingerprint,
           "compat": manifest.compatibility_identity()[:12],
           "delta_chars": len(delta.render()),
           "delta_tokens": manifest.contract_delta_estimated_tokens,
           "stable_tokens": manifest.stable_prefix_estimated_tokens,
           "num_ctx": int(gen.num_ctx)}
    t0 = time.monotonic()
    first_ms = None
    pe_count = pe_ms = load_ms = None
    try:
        async for ch in chat_stream(
            model=route.model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": prompt}],
            think=route.think, max_tokens=int(gen.num_predict),
            temperature=float(gen.temperature), budget=budget, timeouts=timeouts,
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
        row["error"] = type(exc).__name__
    row["first_ms"] = first_ms
    row["total_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
    row["prompt_eval_count"] = pe_count
    row["prompt_eval_ms"] = pe_ms
    row["load_ms"] = load_ms
    state = get_prefix_cache_observer().classify(
        compatibility_identity=manifest.compatibility_identity(),
        prompt_eval_count=pe_count, prompt_eval_ms=pe_ms, load_ms=load_ms,
        first_content_ms=first_ms)
    row["cache_state"] = state.value
    return row


async def _run(quick: bool) -> None:
    prompts = [(l, p) for (l, p) in PROMPTS if not quick or l in QUICK]
    print("=" * 78)
    print("JARVIS V69 M58 — bounded live prefix-parity benchmark (observe-only)")
    print("=" * 78)
    resident = await _residency()
    print(f"resident models: {resident or 'none observed'}")
    # Warm the CONCISE family (the shared stable prefix), through the real seam.
    try:
        from core.contract_family import ContractFamily, get_family_prewarm
        fp = get_family_prewarm()
        rec = await fp.warm_family(ContractFamily.CONCISE)
        print(f"family prewarm CONCISE -> state={rec.state.value} "
              f"first_token_ms={rec.first_token_ms} prompt_eval_ms={rec.prompt_eval_ms} "
              f"warmed_identity={(fp.warmed_identity() or '')[:12]}")
    except Exception as exc:  # noqa: BLE001
        print(f"family prewarm skipped: {type(exc).__name__}")
    print("-" * 78)
    hdr = ("label", "contract", "ctx", "dΔtok", "1st_ms", "peCount", "pe_ms",
           "load_ms", "cache_state")
    print("{:<12}{:<10}{:>5}{:>7}{:>8}{:>9}{:>9}{:>9}  {}".format(*hdr))
    rows = []
    for label, prompt in prompts:
        row = await _measure(label, prompt)
        rows.append(row)
        print("{:<12}{:<10}{:>5}{:>7}{:>8}{:>9}{:>9}{:>9}  {}".format(
            row["label"], row["contract"], row["num_ctx"], row["delta_tokens"],
            row.get("first_ms") or "-", row.get("prompt_eval_count") or "-",
            row.get("prompt_eval_ms") or "-", row.get("load_ms") or "-",
            row.get("cache_state", "-")))
    print("-" * 78)
    # Prove stable-prefix reuse: same stable fingerprint across every FAST turn.
    fps = {r["stable_fp"] for r in rows}
    print(f"distinct stable-prefix fingerprints across the run: {len(fps)} "
          f"(1 = fully shared)")
    ctxs = {r["num_ctx"] for r in rows}
    print(f"distinct num_ctx across the run: {len(ctxs)} (1 = prewarm parity held)")
    from core.prefix_cache import get_prefix_cache_observer
    print(f"observer: {get_prefix_cache_observer().snapshot()}")
    print("=" * 78)


def main() -> None:
    ap = argparse.ArgumentParser(description="M58 bounded live prefix benchmark")
    ap.add_argument("--quick", action="store_true", help="run the 3-prompt subset")
    args = ap.parse_args()
    asyncio.run(_run(args.quick))


if __name__ == "__main__":
    main()
