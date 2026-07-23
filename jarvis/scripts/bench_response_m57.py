"""scripts/bench_response_m57.py — V69 M57.9: bounded live response benchmark.

Measures the ADAPTIVE RESPONSE PIPELINE against the real host, through the real
seams: contract selection, generation budget, native Ollama transport, the
sentence-aware assembler and the speech planner.

SAFETY POSTURE — what this script will never do
-----------------------------------------------
  * no model is pulled and no model is downloaded;
  * no Ollama setting is written and the server is never restarted;
  * no semantic collection is read, written or reindexed;
  * no tool runs, no filesystem path is opened, no network host but loopback
    Ollama is contacted;
  * TTS is planned but never synthesized (the planner is pure).

It is a MEASUREMENT, not a turn: nothing is written to conversation history,
runtime health or semantic memory.

Usage (from the repo root)::

    python jarvis/scripts/bench_response_m57.py
    python jarvis/scripts/bench_response_m57.py --quick
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Bounded prompt set: the M57 acceptance scenarios, nothing expensive.
PROMPTS: list[tuple[str, str]] = [
    ("greeting", "hola"),
    ("simple-howto", "como saco la raiz cuadrada de algo"),
    ("brief-override", "explicame POO brevemente"),
    ("standard", "explicame herencia en Python con un ejemplo"),
    ("detail-override", "explica Kerberos con mas detalle"),
    ("english", "answer in English: what is polymorphism"),
]
QUICK = {"greeting", "simple-howto", "brief-override"}


async def _bench_one(label: str, prompt: str) -> dict:
    from core.config import settings
    from core.fast_path import decide_fast_route
    from core.generation_budget import (
        budget_for_shape, get_throughput_tracker, hit_generation_cap,
    )
    from core.model_router import ModelDecision, ModelRole, resolve_inference_model
    from core.ollama_native import (
        CancellationToken, NativeTransportError, chat_stream, get_native_capability,
    )
    from core.response_contract import select_contract
    from core.runtime_profile import get_runtime_profile
    from core.speech_stream import build_planner
    from core.stream_assembler import build_assembler
    from core.turn_budget import StageTimeouts, TurnBudget, budget_for, timeouts_for
    from core.turn_policy import classify_request

    policy = classify_request(prompt)
    language = "en" if "English" in prompt else "es"
    decision = ModelDecision(role=ModelRole.FAST, provider="ollama",
                             model=resolve_inference_model(
                                 ModelDecision(role=ModelRole.FAST, provider="ollama",
                                               model="", complexity=0.1, reason="b",
                                               requires_verification=False)),
                             complexity=0.1, reason="bench",
                             requires_verification=False)
    shape = select_contract(prompt, turn_policy=policy, model_decision=decision,
                            language=language,
                            power_policy=get_runtime_profile().policy())
    gen = budget_for_shape(shape, settings=settings,
                           throughput=get_throughput_tracker())
    route = decide_fast_route(turn_policy=policy, model_decision=decision,
                              routed_model=decision.model,
                              native_state=get_native_capability().state.value,
                              settings=settings)

    budget = TurnBudget(total_s=budget_for(policy))
    stage: StageTimeouts = timeouts_for(policy)
    t0 = time.monotonic()
    asm = build_assembler(settings=settings, started_at=t0)
    planner = build_planner(shape=shape, turn_id=0, settings=settings,
                            started_at=t0)

    out = {
        "label": label, "contract": shape.contract.value,
        "reason": shape.reason.value, "language": shape.language,
        "token_budget": gen.num_predict, "num_ctx": gen.num_ctx,
        "adjust": gen.adjustment_reason,
    }
    first_token_ms = None
    first_fragment_ms = None
    first_sentence_ms = None
    first_utterance_ms = None
    fragments = 0
    chunks = 0
    eval_count = None
    tok_s = None
    done_reason = None
    system = ("You are JARVIS. Answer directly and concisely.\n\n"
              + shape.style_directive())
    try:
        async for ch in chat_stream(
            model=route.model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": prompt}],
            think=route.think, max_tokens=gen.num_predict,
            temperature=gen.temperature, budget=budget, timeouts=stage,
            cancellation=CancellationToken(), ctx=gen.num_ctx,
            keep_alive=gen.keep_alive, options_extra=gen.options(),
        ):
            if ch.content:
                chunks += 1
                if first_token_ms is None:
                    first_token_ms = round((time.monotonic() - t0) * 1000, 1)
                for frag in asm.push(ch.content, now=time.monotonic()):
                    fragments += 1
                    if first_fragment_ms is None:
                        first_fragment_ms = round((time.monotonic() - t0) * 1000, 1)
                    if frag.kind.value == "SENTENCE" and first_sentence_ms is None:
                        first_sentence_ms = round((time.monotonic() - t0) * 1000, 1)
                    if planner.plan(frag, pending=0, now=time.monotonic()) \
                            and first_utterance_ms is None:
                        first_utterance_ms = round((time.monotonic() - t0) * 1000, 1)
            if ch.done:
                done_reason = ch.done_reason
                eval_count = ch.eval_count
                tok_s = ch.tokens_per_second()
                break
        for frag in asm.finish(now=time.monotonic()):
            fragments += 1
    except NativeTransportError as exc:
        out["error"] = exc.reason
    except Exception as exc:  # noqa: BLE001
        out["error"] = type(exc).__name__

    total_ms = round((time.monotonic() - t0) * 1000, 1)
    snap = asm.snapshot()
    out.update({
        "first_token_ms": first_token_ms,
        "first_fragment_ms": first_fragment_ms,
        "first_sentence_ms": first_sentence_ms,
        "first_utterance_ms": first_utterance_ms,
        "total_ms": total_ms,
        "chunks": chunks, "fragments": fragments,
        "eval_count": eval_count, "tok_s": tok_s,
        "done_reason": done_reason,
        "truncated": hit_generation_cap(done_reason, eval_count, gen.num_predict),
        "max_buffer": snap["max_buffer_chars"],
        "dupes": snap["duplicate_fragments_suppressed"],
        "spoken": planner.snapshot()["queued"],
    })
    if tok_s:
        get_throughput_tracker().record(tokens_per_second=tok_s,
                                        first_token_ms=first_token_ms)
    return out


def _fmt(v, width=7):
    return str(v if v is not None else "-").rjust(width)


async def main_async(quick: bool) -> int:
    from core.ollama_native import refresh_native_capability
    from core.model_router import ModelRole, model_for_role
    model = model_for_role(ModelRole.FAST) or "qwen3:8b"
    print(f"M57 RESPONSE BENCHMARK — model={model}")
    cap = await refresh_native_capability(model=model)
    print(f"  native={cap.state.value} version={cap.server_version} "
          f"think_false_supported={cap.think_false_supported}")
    if not cap.native_chat_reachable:
        print("  native transport unreachable — nothing measured, nothing changed")
        return 1
    rows = []
    for label, prompt in PROMPTS:
        if quick and label not in QUICK:
            continue
        row = await _bench_one(label, prompt)
        rows.append(row)
        print("  {:<16} {:<10} budget={:<4} ctx={:<5} 1st_tok={} 1st_frag={} "
              "1st_sent={} total={} tok/s={} eval={} trunc={}".format(
                  row["label"], row["contract"], row["token_budget"], row["num_ctx"],
                  _fmt(row.get("first_token_ms")), _fmt(row.get("first_fragment_ms")),
                  _fmt(row.get("first_sentence_ms")), _fmt(row.get("total_ms"), 8),
                  _fmt(row.get("tok_s"), 5), _fmt(row.get("eval_count"), 4),
                  row.get("truncated")))
        if row.get("error"):
            print(f"      error={row['error']}")
    ok = [r for r in rows if r.get("first_token_ms")]
    if ok:
        print("\nSUMMARY")
        print(f"  turns={len(ok)}")
        print(f"  first token  min/median/max = "
              f"{min(r['first_token_ms'] for r in ok):.0f} / "
              f"{sorted(r['first_token_ms'] for r in ok)[len(ok) // 2]:.0f} / "
              f"{max(r['first_token_ms'] for r in ok):.0f} ms")
        speeds = [r["tok_s"] for r in ok if r.get("tok_s")]
        if speeds:
            print(f"  throughput   min/median/max = {min(speeds):.1f} / "
                  f"{sorted(speeds)[len(speeds) // 2]:.1f} / {max(speeds):.1f} tok/s")
        print(f"  fragments/turn = "
              f"{sum(r['fragments'] for r in ok) / len(ok):.1f} "
              f"(chunks/turn = {sum(r['chunks'] for r in ok) / len(ok):.1f})")
        print(f"  max buffer = {max(r['max_buffer'] for r in ok)} chars")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="M57 bounded live response benchmark")
    ap.add_argument("--quick", action="store_true",
                    help="only the three cheapest prompts")
    args = ap.parse_args()
    return asyncio.run(main_async(args.quick))


if __name__ == "__main__":
    raise SystemExit(main())
