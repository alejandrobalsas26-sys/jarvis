"""core/runtime_health.py — V67 M34: unified runtime & collector health snapshot.

One coherent, READ-ONLY health view that COMPOSES the existing diagnostics rather than
replacing them — the M28 collector fabric, the resource reading, the performance
profiler, the health watchdog, the model runtime resolution and the operational spine.
It does not add a monitoring stack; it is a bounded, structured, in-memory snapshot.

Non-blocking by construction: it only reads already-computed state (fabric counters,
profiler samples, watchdog dicts, resolved role models) and takes a single non-blocking
CPU/RAM sample. It never runs a self-test, never probes Ollama, and never touches the
event loop — safe to call while a DEEP inference is running.

Statuses reuse the fabric's vocabulary (:class:`~core.collector_fabric.CollectorStatus`):
OK / WARMING / DORMANT / OPTIONAL / DEGRADED / FAILED / STOPPING / BACKPRESSURE. DORMANT
and OPTIONAL are NOT failures — an unconfigured collector or an unmeasured metric is
reported honestly as such, never as "healthy and fine".
"""
from __future__ import annotations

from dataclasses import dataclass, field

from core.collector_fabric import CollectorStatus as HealthStatus
from core.ops_views import _MAX_LIST, _redact

# Worst-of ranking. DORMANT/OPTIONAL are healthy (rank 0) — they never degrade overall.
_STATUS_RANK: dict[HealthStatus, int] = {
    HealthStatus.DORMANT: 0, HealthStatus.OPTIONAL: 0, HealthStatus.OK: 1,
    HealthStatus.WARMING: 2, HealthStatus.STOPPING: 3, HealthStatus.BACKPRESSURE: 4,
    HealthStatus.DEGRADED: 5, HealthStatus.FAILED: 6,
}
_UNHEALTHY = frozenset({HealthStatus.DEGRADED, HealthStatus.FAILED,
                        HealthStatus.BACKPRESSURE, HealthStatus.STOPPING})


def _healthy(status: HealthStatus) -> bool:
    return status not in _UNHEALTHY


@dataclass
class SubsystemHealth:
    name: str
    status: HealthStatus
    detail: str = ""
    metrics: dict = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        return _healthy(self.status)

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status.value, "healthy": self.healthy,
                "detail": _redact(self.detail), "metrics": self.metrics}


@dataclass
class RuntimeHealthSnapshot:
    overall: HealthStatus
    subsystems: list[SubsystemHealth]
    metrics: dict = field(default_factory=dict)

    @property
    def degraded(self) -> list[str]:
        return [s.name for s in self.subsystems if not s.healthy]

    def to_dict(self) -> dict:
        return {"panel": "runtime_health", "overall": self.overall.value,
                "healthy": _healthy(self.overall),
                "degraded": self.degraded,
                "subsystems": [s.to_dict() for s in self.subsystems],
                "metrics": self.metrics}

    def summary(self) -> str:
        """A compact ASCII one-liner (Windows/TTS-safe)."""
        parts = [f"{s.name}={s.status.value}" for s in self.subsystems]
        return f"RUNTIME {self.overall.value.upper()}: " + ", ".join(parts)


# ══════════════════════════════════════════════════════════════════════════════
#  Subsystem builders (pure — take already-fetched raw data)
# ══════════════════════════════════════════════════════════════════════════════
def _collectors_subsystem(fm: dict, telemetry: dict | None = None) -> SubsystemHealth:
    fm = fm or {}
    total, active = fm.get("total", 0), fm.get("active", 0)
    failed, backp = fm.get("failed", 0), fm.get("backpressure", 0)
    if failed:
        status = HealthStatus.FAILED if active == 0 else HealthStatus.DEGRADED
    elif backp:
        status = HealthStatus.BACKPRESSURE
    elif active:
        status = HealthStatus.OK
    else:
        status = HealthStatus.DORMANT
    events, drops = fm.get("events_emitted", 0), fm.get("drops", 0)
    drop_ratio = round(drops / max(1, events + drops), 4)
    metrics = {"total": total, "active": active, "dormant": fm.get("dormant", 0),
               "degraded": fm.get("degraded", 0), "failed": failed, "backpressure": backp,
               "events_emitted": events, "queue_drops": drops, "drop_ratio": drop_ratio}
    tel_extra = _telemetry_rollup(telemetry)
    if tel_extra:
        metrics.update(tel_extra)
    return SubsystemHealth(
        "collectors", status,
        f"{active} active / {fm.get('dormant', 0)} dormant / {failed} failed of {total}",
        metrics)


def _telemetry_rollup(telemetry: dict | None) -> dict:
    """M39: fold per-collector telemetry into a bounded fabric-wide summary — a count
    per derived state and the peak event rate / lag — without unbounded per-collector
    fan-out into the metrics map."""
    if not telemetry:
        return {}
    states: dict[str, int] = {}
    peak_eps = 0.0
    max_lag = 0.0
    for snap in telemetry.values():
        if not isinstance(snap, dict):
            continue
        st = snap.get("state", "unknown")
        states[st] = states.get(st, 0) + 1
        peak_eps = max(peak_eps, snap.get("events_per_second") or 0.0)
        max_lag = max(max_lag, snap.get("median_lag_s") or 0.0)
    return {"telemetry_states": states, "telemetry_peak_eps": round(peak_eps, 4),
            "telemetry_max_lag_s": round(max_lag, 3)}


def _resource_subsystem(res: dict | None) -> SubsystemHealth:
    if not res:
        return SubsystemHealth("resource", HealthStatus.OPTIONAL,
                               "not measured (psutil unavailable)", {})
    cpu, ram = res.get("cpu_percent"), res.get("ram_percent")
    status = HealthStatus.OK
    if (cpu is not None and cpu >= 90) or (ram is not None and ram >= 90):
        status = HealthStatus.DEGRADED
    elif (cpu is not None and cpu >= 75) or (ram is not None and ram >= 88):
        status = HealthStatus.WARMING
    return SubsystemHealth("resource", status,
                           f"cpu {cpu}% / ram {ram}%",
                           {"cpu_percent": cpu, "ram_percent": ram})


def _tasks_subsystem(watchdog_status: dict | None) -> SubsystemHealth:
    ws = watchdog_status or {}
    if not ws:
        return SubsystemHealth("tasks", HealthStatus.DORMANT, "no supervised tasks", {})
    down = [n for n, st in ws.items()
            if str(st).lower() in ("done", "dead", "failed", "restarting")]
    status = HealthStatus.DEGRADED if down else HealthStatus.OK
    return SubsystemHealth("tasks", status,
                           f"{len(ws) - len(down)} running / {len(down)} down",
                           {"supervised": len(ws), "down": down[:_MAX_LIST]})


def _inference_subsystem(profiler: dict | None) -> SubsystemHealth:
    stats = profiler or {}
    inf = _pick_stat(stats, ("llm_inference", "inference", "chat", "generate"))
    load = _pick_stat(stats, ("model_load", "load_model", "warmup"))
    if not inf and not load:
        return SubsystemHealth("inference", HealthStatus.DORMANT,
                               "no inference samples yet", {})
    metrics = {}
    if inf:
        metrics.update({"inference_p50_s": inf.get("p50_s"),
                        "inference_p95_s": inf.get("p95_s"),
                        "inference_count": inf.get("count")})
    if load:
        metrics.update({"model_load_p50_s": load.get("p50_s"),
                        "model_load_max_s": load.get("max_s")})
    return SubsystemHealth("inference", HealthStatus.OK,
                           f"p95 {metrics.get('inference_p95_s', '?')}s over "
                           f"{metrics.get('inference_count', 0)} call(s)", metrics)


def _model_subsystem(model: dict | None) -> SubsystemHealth:
    roles = (model or {}).get("roles", {})
    status = HealthStatus.OK if roles else HealthStatus.DEGRADED
    return SubsystemHealth("model_runtime", status,
                           f"{len(roles)} role(s) resolved; probe={model.get('probe') if model else 'n/a'}",
                           {"roles": roles})


def _verifier_subsystem(verifier: dict | None = None) -> SubsystemHealth:
    """V68.1 M49 — bounded CPU-aware verification latency observability."""
    stats = verifier if verifier is not None else _live_verifier()
    count = stats.get("count", 0)
    if not count:
        return SubsystemHealth("verifier", HealthStatus.DORMANT,
                               "no verification samples yet", {})
    timeouts = stats.get("timeouts", 0)
    # Frequent timeouts => the CPU verifier is struggling; surface as DEGRADED.
    status = HealthStatus.DEGRADED if timeouts and timeouts >= max(1, count // 2) \
        else HealthStatus.OK
    return SubsystemHealth(
        "verifier", status,
        f"avg {stats.get('avg_s', 0)}s / max {stats.get('max_s', 0)}s over "
        f"{count} pass(es), {timeouts} timeout(s)",
        {"verifier_avg_s": stats.get("avg_s"), "verifier_max_s": stats.get("max_s"),
         "verifier_last_s": stats.get("last_s"), "verifier_timeouts": timeouts,
         "verifier_count": count},
    )


def _interactive_subsystem(turn: dict | None = None, life: dict | None = None,
                           console: dict | None = None,
                           tts: dict | None = None) -> SubsystemHealth:
    """V69 M54 — interactive-runtime observability: end-to-end turn latency (M54.5),
    lifecycle phase timings (M54.2), console queue health (M54.1) and TTS governor
    backpressure (M54.9). DORMANT until the first turn; DEGRADED only if the runtime
    itself has FAILED."""
    turn = turn if turn is not None else _live_turn()
    life = life if life is not None else _live_lifecycle()
    console = console if console is not None else _live_console()
    tts = tts if tts is not None else {}
    state = str(life.get("state", "UNKNOWN"))
    turns = turn.get("count", 0) or 0
    expired = turn.get("expired", 0) or 0
    # Status reflects TURN-BUDGET health only — lifecycle state (STOPPING/…) is
    # informational (a normal transient), never a health failure, so a shutting
    # -down process is not reported as "unhealthy runtime".
    if not turns:
        status = HealthStatus.DORMANT
    elif expired >= max(1, turns // 2):
        status = HealthStatus.DEGRADED   # many turns blew their end-to-end budget
    else:
        status = HealthStatus.OK
    # V69 M54.1.13 — the metrics that would have made the live failures visible:
    # WHICH stage timed out, whether cancellation actually worked, whether the
    # lifecycle's TEXT_READY claim matches a real reader, and FAST's true state.
    fast = _live_fast_readiness()
    metrics = {
        "turn_avg_total_ms": turn.get("avg_total_ms"),
        "turn_max_total_ms": turn.get("max_total_ms"),
        "turn_count": turn.get("count"),
        "turn_expired": turn.get("expired"),
        "turn_timed_out": turn.get("timed_out"),
        "turn_last_timeout_stage": turn.get("last_timeout_stage"),
        "turn_cancellations": turn.get("cancellations"),
        "turn_last_first_token_ms": turn.get("last_first_token_ms"),
        "text_ready_ms": life.get("text_ready_ms"),
        "core_ready_ms": life.get("core_ready_ms"),
        "operational_ready_ms": life.get("operational_ready_ms"),
        # M54.1.9 — these two disagreeing IS the false-TEXT_READY bug.
        "input_available": life.get("input_available"),
        "fast_state": fast.get("state"),
        "fast_model": fast.get("model"),
        "fast_last_probe_ms": fast.get("last_probe_ms"),
        "fast_last_error": fast.get("last_error"),
        "greeting_renderer": "deterministic",
        "greeting_unresolved_placeholders": 0,
        "console_dropped": console.get("dropped"),
        "console_coalesced": console.get("coalesced"),
        "tts_dropped": tts.get("dropped"),
        "tts_coalesced": tts.get("coalesced"),
    }
    return SubsystemHealth(
        "interactive", status,
        f"lifecycle={state}; turns={turn.get('count', 0)}; "
        f"turn_max={turn.get('max_total_ms', 0)}ms; fast={fast.get('state')}",
        metrics)


def _fast_inference_subsystem(fast_stats: dict | None = None) -> SubsystemHealth:
    """V69 M55.13 — native FAST no-think transport latency + no-think capability.
    DORMANT until the first FAST turn; DEGRADED only if timeouts dominate."""
    stats = fast_stats if fast_stats is not None else _live_fast_inference()
    requests = stats.get("requests", 0) or 0
    timeouts = stats.get("timeouts", 0) or 0
    if not requests:
        status = HealthStatus.DORMANT
    elif requests >= 4 and timeouts >= max(1, requests // 2):
        # Only a SUSTAINED timeout rate degrades — one timeout in a handful of
        # turns is not a degraded transport (and avoids flapping on tiny samples).
        status = HealthStatus.DEGRADED
    else:
        status = HealthStatus.OK
    return SubsystemHealth(
        "fast_inference", status,
        "transport={} model={} think_supported={} p50_ft={}ms reqs={}".format(
            stats.get("active_transport"), stats.get("active_model"),
            stats.get("think_supported"), stats.get("p50_first_token_ms"), requests),
        {k: v for k, v in stats.items() if v is not None})


def _ollama_env_subsystem(env: dict | None = None) -> SubsystemHealth:
    """V69 M55.13 — truthful Ollama environment. Advisory ONLY: an unverified /
    not-applied OLLAMA_MAX_LOADED_MODELS never marks the runtime unhealthy."""
    e = env if env is not None else _live_ollama_env()
    if not e:
        return SubsystemHealth("ollama_env", HealthStatus.OPTIONAL,
                               "ollama env not available", {})
    cfg = e.get("configured_by_jarvis", {}) or {}
    metrics = {
        "configured_parallel": cfg.get("num_parallel"),
        "configured_max_loaded": cfg.get("max_loaded_models"),
        "observed_loaded_models": e.get("observed_loaded_models"),
        "settings_verified": e.get("settings_verified"),
        "server_version": e.get("server_version"),
        "capability_probe_state": e.get("capability_probe_state"),
        "max_loaded_applied": e.get("max_loaded_applied"),
        "active_transport": e.get("active_transport"),
    }
    # Advisory only (rank 0) — the truthful env view informs the operator but must
    # never degrade or even shift the overall runtime verdict.
    return SubsystemHealth("ollama_env", HealthStatus.OPTIONAL,
                           str(e.get("max_loaded_applied", "")), metrics)


def _response_pipeline_subsystem(response: dict | None = None,
                                 stream: dict | None = None,
                                 speech: dict | None = None,
                                 context: dict | None = None,
                                 quality: dict | None = None) -> SubsystemHealth:
    """V69 M57 — the adaptive response pipeline.

    ONE advisory subsystem (rank 0) covering contract selection, streaming,
    progressive speech, conversation context, interruption and output quality.
    Advisory for the same reason the residency view is: a slow answer or a dropped
    utterance is a comfort problem, not a runtime fault, and must never degrade the
    overall verdict.

    Every metric is a counter, a timing or an enum. No prompt, answer, memory
    record, secret or token can appear here — the collectors below read only
    already-bounded snapshots that were built content-free at the source.
    """
    r = response if response is not None else _live_response_runtime()
    s = stream if stream is not None else {}
    sp = speech if speech is not None else _live_speech_metrics()
    c = context if context is not None else _live_context_metrics()
    q = quality if quality is not None else _live_quality_counters()
    if not (r or s or sp or c or q):
        return SubsystemHealth("response_pipeline", HealthStatus.OPTIONAL,
                               "no interactive turn yet", {})
    if not s:
        s = (sp or {}).get("stream", {}) or {}
    turn = (r or {}).get("current_turn") or {}
    tp = (r or {}).get("throughput", {}) or {}
    metrics = {
        # contract
        "selected_contract": turn.get("contract"),
        "selection_reason": turn.get("selection_reason"),
        "token_budget": turn.get("token_budget"),
        "context_budget": turn.get("context_budget"),
        "profile": (r or {}).get("profile"),
        # stream
        "chunks_received": s.get("chunks_received"),
        "fragments_emitted": s.get("fragments_emitted"),
        "first_fragment_ms": turn.get("first_fragment_ms") or s.get("first_fragment_ms"),
        "first_sentence_ms": turn.get("first_sentence_ms") or s.get("first_sentence_ms"),
        "duplicate_fragments_suppressed": s.get("duplicate_fragments_suppressed"),
        "max_buffer_chars": s.get("max_buffer_chars"),
        "terminal_state": turn.get("state"),
        # speech
        "progressive_enabled": (sp or {}).get("progressive_enabled"),
        "first_utterance_ms": turn.get("first_utterance_ms")
        or (sp or {}).get("first_utterance_ms"),
        "utterances_queued": (sp or {}).get("queued"),
        "utterances_coalesced": (sp or {}).get("split_utterances"),
        "utterances_stale_dropped": (sp or {}).get("stale_dropped"),
        "speech_depth": (sp or {}).get("current_depth"),
        "speech_high_watermark": (sp or {}).get("high_watermark"),
        "muted": (r or {}).get("muted"),
        # context
        "estimated_total_tokens": (c or {}).get("estimated_total_tokens"),
        "recent_turn_tokens": (c or {}).get("recent_turn_tokens"),
        "digest_tokens": (c or {}).get("digest_tokens"),
        "tool_evidence_tokens": (c or {}).get("tool_evidence_tokens"),
        "memory_evidence_tokens": (c or {}).get("memory_evidence_tokens"),
        "trimmed_items": (c or {}).get("trimmed_items"),
        "digest_age_turns": (c or {}).get("digest_age_turns"),
        # interruption
        "interrupted_turns": (r or {}).get("interrupted_turns"),
        "replaced_turns": (r or {}).get("replaced_turns"),
        "cancellation_latency_ms": (r or {}).get("cancellation_latency_ms"),
        "late_chunks_suppressed": (r or {}).get("late_chunks_suppressed"),
        # quality
        "repetition_suppressions": (q or {}).get("repetition_suppressions"),
        "placeholder_blocks": (q or {}).get("placeholder_blocks"),
        "incomplete_format_repairs": (q or {}).get("incomplete_format_repairs"),
        "continuation_offers": (r or {}).get("continuation_offers"),
        "truncated_turns": (r or {}).get("truncated_turns"),
        # throughput
        "throughput_tok_s": tp.get("median_tok_s"),
        "throughput_samples": tp.get("samples"),
    }
    detail = "contract={} throughput={} tok/s".format(
        turn.get("contract") or "n/a", tp.get("median_tok_s") or "n/a")
    return SubsystemHealth("response_pipeline", HealthStatus.OPTIONAL, detail,
                           metrics)


def _live_response_runtime() -> dict:
    try:
        from core.response_runtime import get_response_runtime
        return get_response_runtime().snapshot()
    except Exception:  # noqa: BLE001
        return {}


def _live_speech_metrics() -> dict:
    try:
        from core.speech_stream import last_speech_metrics
        return last_speech_metrics()
    except Exception:  # noqa: BLE001
        return {}


def _live_context_metrics() -> dict:
    try:
        from core.context_composer import last_context_metrics
        return last_context_metrics()
    except Exception:  # noqa: BLE001
        return {}


def _live_quality_counters() -> dict:
    try:
        from core.response_quality import quality_counters
        return quality_counters()
    except Exception:  # noqa: BLE001
        return {}


def _prompt_cache_subsystem(prompt: dict | None = None, cache: dict | None = None,
                            prewarm: dict | None = None, compaction: dict | None = None,
                            tools: dict | None = None, barge: dict | None = None,
                            response: dict | None = None) -> SubsystemHealth:
    """V69 M58.9 — prompt prefix parity, cache-safe prewarm and real-time interruption.

    Advisory ONLY (rank 0): a first-use prefill cost, a cold prefix or a COMMAND_ONLY
    barge-in mode is a PERFORMANCE/comfort fact, never a runtime fault, so it must not
    degrade the overall verdict. Every metric is a fingerprint, a count, a millisecond
    or an enum — NEVER a prompt, an answer, a tool argument or a key value.
    """
    p = prompt if prompt is not None else _live_prompt_manifest()
    c = cache if cache is not None else _live_prefix_cache()
    pw = prewarm if prewarm is not None else _live_family_prewarm()
    cp = compaction if compaction is not None else _live_compaction()
    tl = tools if tools is not None else _live_tool_metrics()
    bi = barge if barge is not None else _live_barge_in()
    r = response if response is not None else _live_response_runtime()
    sw = _live_session_warmth()
    if not (p or c or pw or cp or tl or bi):
        return SubsystemHealth("prompt_cache", HealthStatus.OPTIONAL,
                               "no interactive turn yet", {})
    size = (p or {}).get("size", {}) or {}
    metrics = {
        # ── prompt manifest ──
        "core_fingerprint": (p or {}).get("core_fingerprint"),
        "session_fingerprint": (p or {}).get("session_fingerprint"),
        "contract_schema_version": (p or {}).get("contract_schema_version"),
        "stable_prefix_estimated_tokens": (p or {}).get("stable_prefix_estimated_tokens"),
        "contract_delta_estimated_tokens": (p or {}).get("contract_delta_estimated_tokens"),
        "compatibility_identity": (p or {}).get("compatibility_identity"),
        "duplicate_sections_removed": size.get("duplicate_sections_removed"),
        "prompt_budget_used": size.get("total_tokens"),
        "prompt_budget_capacity": size.get("budget_tokens"),
        # ── prefix reuse ──
        "cache_state": (c or {}).get("cache_state"),
        "invalidations": (c or {}).get("invalidations"),
        "last_invalidation_reason": (c or {}).get("last_invalidation_reason"),
        "recent_prompt_eval_ms": (c or {}).get("recent_prompt_eval_ms"),
        "warm_prompt_eval_ms": (c or {}).get("warm_prompt_eval_ms"),
        "cold_prompt_eval_ms": (c or {}).get("cold_prompt_eval_ms"),
        "observed_reuse_ratio": (c or {}).get("observed_reuse_ratio"),
        # ── prewarm ──
        "prewarm_mode": (pw or {}).get("mode"),
        "warmed_families": (pw or {}).get("family_states"),
        "prewarm_attempts": (pw or {}).get("attempts"),
        "prewarm_successes": (pw or {}).get("successes"),
        "prewarm_cancellations": (pw or {}).get("cancellations"),
        "last_family": (pw or {}).get("last_family"),
        "family_last_first_token_ms": (pw or {}).get("last_first_token_ms"),
        "family_last_prompt_eval_ms": (pw or {}).get("last_prompt_eval_ms"),
        "stale_fingerprints": (pw or {}).get("stale_fingerprints"),
        # ── sampling parity (M59.1) ──
        "prewarm_runner_identity": (pw or {}).get("prewarm_runner_identity"),
        "live_runner_identity": (pw or {}).get("live_runner_identity"),
        "runner_parity": (pw or {}).get("runner_parity"),
        # ── session warmth & predictive rewarm (M59.2) ──
        "session_warmth_state": (sw or {}).get("session_state"),
        "session_reuse_state": (sw or {}).get("reuse_state"),
        "session_observation_count": (sw or {}).get("observation_count"),
        "session_invalidations": (sw or {}).get("invalidation_count"),
        "predictive_rewarm_attempts": (sw or {}).get("predictive_rewarm_attempts"),
        "predictive_rewarm_successes": (sw or {}).get("predictive_rewarm_successes"),
        "rewarm_cooldown_remaining_s": (sw or {}).get("cooldown_remaining"),
        # ── compaction ──
        "compaction_scheduled": (cp or {}).get("scheduled"),
        "compaction_completed": (cp or {}).get("completed"),
        "compaction_cancelled_for_user": (cp or {}).get("cancelled_for_user"),
        "compaction_validation_failures": (cp or {}).get("validation_failures"),
        "context_tokens_saved": (cp or {}).get("context_tokens_saved"),
        "digest_version": (cp or {}).get("digest_version"),
        "compaction_last_duration_ms": (cp or {}).get("last_duration_ms"),
        # ── tool generation ──
        "tool_rounds": (tl or {}).get("tool_rounds"),
        "tool_malformed_calls": (tl or {}).get("malformed_calls"),
        "tool_denied_calls": (tl or {}).get("denied_calls"),
        "final_response_tokens": (tl or {}).get("final_response_tokens"),
        "tool_schema_fingerprint": (tl or {}).get("tool_schema_fingerprint"),
        "eligible_tool_count": (tl or {}).get("eligible_tool_count"),
        "schema_estimated_tokens": (tl or {}).get("schema_estimated_tokens"),
        # ── barge-in ──
        "barge_in_mode": (bi or {}).get("mode"),
        "barge_in_supported": (bi or {}).get("supported"),
        "active_interruptions": (bi or {}).get("active_interruptions"),
        "command_interruptions": (bi or {}).get("command_interruptions"),
        "barge_in_cancellation_latency_ms": (bi or {}).get("cancellation_latency_ms"),
        "late_chunks_suppressed": (r or {}).get("late_chunks_suppressed"),
        "terminal_restore_failures": (bi or {}).get("terminal_restore_failures"),
    }
    detail = "cache={} prewarm={} barge_in={}".format(
        (c or {}).get("cache_state") or "n/a", (pw or {}).get("mode") or "n/a",
        (bi or {}).get("mode") or "n/a")
    return SubsystemHealth("prompt_cache", HealthStatus.OPTIONAL, detail, metrics)


def _live_prompt_manifest() -> dict:
    try:
        from core.prompt_manifest import last_manifest_metrics
        return last_manifest_metrics()
    except Exception:  # noqa: BLE001
        return {}


def _live_prefix_cache() -> dict:
    try:
        from core.prefix_cache import get_prefix_cache_observer
        return get_prefix_cache_observer().snapshot()
    except Exception:  # noqa: BLE001
        return {}


def _live_family_prewarm() -> dict:
    try:
        from core.contract_family import get_family_prewarm
        return get_family_prewarm().snapshot()
    except Exception:  # noqa: BLE001
        return {}


def _live_session_warmth() -> dict:
    try:
        from core.session_warmth import session_warmth_health
        return session_warmth_health()
    except Exception:  # noqa: BLE001
        return {}


def _live_compaction() -> dict:
    try:
        from core.compaction_scheduler import get_compaction_scheduler
        return get_compaction_scheduler().snapshot()
    except Exception:  # noqa: BLE001
        return {}


def _live_tool_metrics() -> dict:
    try:
        from core.tool_loop import last_tool_metrics
        return last_tool_metrics()
    except Exception:  # noqa: BLE001
        return {}


def _live_barge_in() -> dict:
    try:
        from core.barge_in import get_barge_in_controller
        return get_barge_in_controller().snapshot()
    except Exception:  # noqa: BLE001
        return {}


def _residency_subsystem(residency: dict | None = None, governor: dict | None = None,
                         prewarm: dict | None = None, power: dict | None = None
                         ) -> SubsystemHealth:
    """V69 M56.8 — model residency, inference arbitration, prewarm and power profile.

    Advisory ONLY (rank 0), for the same reason the Ollama env subsystem is: an
    evicted FAST model or a battery-disabled prewarm is a PERFORMANCE fact, not a
    runtime fault, and it must never degrade the overall verdict. Everything here is
    a bounded counter, a state name or a millisecond — never a prompt, a generated
    token or a vector.
    """
    r = residency if residency is not None else _live_residency()
    g = governor if governor is not None else _live_governor()
    p = prewarm if prewarm is not None else _live_prewarm()
    pw = power if power is not None else _live_power()
    if not any((r, g, p, pw)):
        return SubsystemHealth("residency", HealthStatus.OPTIONAL,
                               "residency subsystem not available", {})
    metrics = {
        # ── model residency (observed, never inferred) ──
        "residency_state": r.get("residency_state"),
        "observed_models": ",".join(r.get("observed_models") or []) or None,
        "preferred_models": ",".join(r.get("preferred_models") or []) or None,
        "fast_evictions": r.get("fast_evictions"),
        "embedding_evictions": r.get("embedding_evictions"),
        "restoration_attempts": r.get("restoration_attempts"),
        "restoration_successes": r.get("restoration_successes"),
        "last_switch_reason": r.get("last_switch_reason"),
        "last_observation_at": r.get("last_observation_at"),
        # ── resource governor ──
        "active_role": g.get("active_role"),
        "active_priority": g.get("active_priority"),
        "queue_depth": g.get("queue_depth"),
        "queue_capacity": g.get("queue_capacity"),
        "high_watermark": g.get("high_watermark"),
        "average_wait_ms": g.get("average_wait_ms"),
        "background_deferrals": g.get("background_deferrals"),
        "governor_cancellations": g.get("cancellations"),
        "starvation_preventions": g.get("starvation_preventions"),
        # ── prewarm ──
        "prewarm_mode": p.get("mode"),
        "prewarm_state": p.get("state"),
        "prewarm_model": p.get("model"),
        "prewarm_attempts": p.get("attempts"),
        "prewarm_successes": p.get("successes"),
        "prewarm_failures": p.get("failures"),
        "prewarm_cancellations": p.get("cancellations"),
        "prewarm_last_load_ms": p.get("last_load_ms"),
        "prewarm_last_first_token_ms": p.get("last_first_token_ms"),
        "prewarm_last_total_ms": p.get("last_total_ms"),
        "prewarm_last_failure_reason": p.get("last_failure_reason"),
        # ── power profile ──
        "power_profile": pw.get("profile"),
        "power_source": pw.get("source"),
        "power_detected_at": pw.get("detected_at"),
        "power_override": pw.get("override"),
    }
    detail = "{} prewarm={} power={}".format(
        r.get("residency_state", "UNKNOWN"), p.get("state", "IDLE"),
        pw.get("profile", "UNKNOWN"))
    return SubsystemHealth("residency", HealthStatus.OPTIONAL, detail, metrics)


def _live_residency() -> dict:
    try:
        from core.residency import get_residency_metrics
        return get_residency_metrics().snapshot()
    except Exception:  # noqa: BLE001
        return {}


def _live_governor() -> dict:
    try:
        from core.residency_governor import get_governor
        return get_governor().snapshot()
    except Exception:  # noqa: BLE001
        return {}


def _live_prewarm() -> dict:
    try:
        from core.fast_prewarm import get_fast_prewarm
        return get_fast_prewarm().snapshot()
    except Exception:  # noqa: BLE001
        return {}


def _live_power() -> dict:
    try:
        from core.runtime_profile import get_runtime_profile
        return get_runtime_profile().detect().snapshot()
    except Exception:  # noqa: BLE001
        return {}


def _live_fast_inference() -> dict:
    try:
        from core.fast_readiness import get_fast_readiness
        return get_fast_readiness().fast_inference_snapshot()
    except Exception:  # noqa: BLE001
        return {}


def _live_ollama_env() -> dict:
    try:
        from core.ollama_env import collect_ollama_env
        return collect_ollama_env().snapshot()
    except Exception:  # noqa: BLE001
        return {}


def _live_fast_readiness() -> dict:
    try:
        from core.fast_readiness import get_fast_readiness
        return get_fast_readiness().snapshot()
    except Exception:  # noqa: BLE001
        return {}


def _live_watcher() -> dict:
    try:
        from tools.yara_file_monitor import watcher_metrics
        return watcher_metrics()
    except Exception:  # noqa: BLE001
        return {}


def _filesystem_subsystem(watcher: dict | None = None) -> SubsystemHealth:
    """V69 M54.1.13 — filesystem-watcher backpressure. The live boot printed dozens
    of QueueFull tracebacks and NOTHING recorded them: the YARA monitor was
    TaskWatchdog-registered only, never a health surface, so its drops were
    invisible. DORMANT when the monitor is not running; DEGRADED when a root is
    STALE (events were lost and recovery has not finished) — never silently OK."""
    watcher = watcher if watcher is not None else _live_watcher()
    if not watcher.get("running"):
        return SubsystemHealth("filesystem_watch", HealthStatus.DORMANT,
                               "file monitor not running", {})
    classes = watcher.get("classes", {}) or {}
    rec = watcher.get("reconcile", {}) or {}
    total = {k: 0 for k in ("received", "accepted", "coalesced", "ignored",
                            "dropped", "overflows", "queue_depth",
                            "queue_high_watermark")}
    last_reason = None
    last_overflow = None
    for c in classes.values():
        for k in total:
            total[k] += c.get(k, 0) or 0
        last_reason = c.get("last_drop_reason") or last_reason
        last_overflow = c.get("last_overflow_at") or last_overflow
    stale = rec.get("stale_roots", 0) or 0
    status = HealthStatus.DEGRADED if stale else HealthStatus.OK
    metrics = {
        "watch_queue_depth": total["queue_depth"],
        "watch_queue_high_watermark": total["queue_high_watermark"],
        "watch_received": total["received"],
        "watch_accepted": total["accepted"],
        "watch_coalesced": total["coalesced"],
        "watch_ignored": total["ignored"],
        "watch_dropped": total["dropped"],
        "watch_overflows": total["overflows"],
        "watch_last_drop_reason": last_reason,
        "watch_last_overflow_at": last_overflow,
        "watch_reconciliations": rec.get("reconciliations", 0),
        "watch_stale_roots": stale,
    }
    return SubsystemHealth(
        "filesystem_watch", status,
        f"queue={total['queue_depth']}/{total['queue_high_watermark']} peak; "
        f"coalesced={total['coalesced']}; dropped={total['dropped']}; "
        f"stale_roots={stale}",
        metrics)


def _spine_subsystem(spine: dict | None) -> SubsystemHealth:
    s = spine or {}
    vs = s.get("verification_success_rate")
    return SubsystemHealth("spine", HealthStatus.OK,
                           f"{s.get('correlation_findings', 0)} finding(s), "
                           f"{s.get('incidents_open', 0)} open incident(s)",
                           {"correlation_findings": s.get("correlation_findings", 0),
                            "incidents_open": s.get("incidents_open", 0),
                            "verification_success_rate": vs})


def _pick_stat(stats: dict, keys: tuple[str, ...]) -> dict | None:
    for k in keys:
        if k in stats and stats[k].get("count"):
            return stats[k]
    # substring fallback (op names vary)
    for name, v in (stats or {}).items():
        if any(k in str(name).lower() for k in keys) and v.get("count"):
            return v
    return None


# ══════════════════════════════════════════════════════════════════════════════
#  Composition
# ══════════════════════════════════════════════════════════════════════════════
def collect_runtime_health(*, fabric_metrics: dict | None = None,
                           watchdog_status: dict | None = None,
                           resource: dict | None = None, profiler: dict | None = None,
                           model: dict | None = None, spine: dict | None = None,
                           telemetry: dict | None = None
                           ) -> RuntimeHealthSnapshot:
    """Compose one health snapshot from already-fetched raw data. Every arg may be
    injected (tests) or left None to read the live source (guarded)."""
    fm = fabric_metrics if fabric_metrics is not None else _live_fabric_metrics()
    ws = watchdog_status if watchdog_status is not None else _live_watchdog_status()
    res = resource if resource is not None else _live_resource()
    prof = profiler if profiler is not None else _live_profiler()
    mdl = model if model is not None else _live_model()
    sp = spine if spine is not None else _live_spine()
    tel = telemetry if telemetry is not None else _live_telemetry()

    subsystems = [
        _collectors_subsystem(fm, tel), _resource_subsystem(res), _tasks_subsystem(ws),
        _inference_subsystem(prof), _model_subsystem(mdl), _spine_subsystem(sp),
        _verifier_subsystem(), _interactive_subsystem(),
        # V69 M54.1.13 — watcher backpressure joins the existing health surface
        # rather than becoming a second one.
        _filesystem_subsystem(),
        # V69 M55.13 — native FAST transport latency + truthful Ollama env state.
        _fast_inference_subsystem(), _ollama_env_subsystem(),
        # V69 M56.8 — model residency, inference arbitration, prewarm, power profile.
        # Advisory: it extends this one health surface rather than adding a second.
        _residency_subsystem(),
        # V69 M57 — ONE advisory response-pipeline subsystem (contract, stream,
        # speech, context, interruption, quality). No new registry: the existing
        # single health collector gains one more entry.
        _response_pipeline_subsystem(),
        # V69 M58.9 — ONE advisory prompt/prefix-cache subsystem (prompt manifest,
        # prefix reuse, family prewarm, idle compaction, tool bounds, barge-in).
        _prompt_cache_subsystem(),
    ]
    overall = max(subsystems, key=lambda s: _STATUS_RANK.get(s.status, 0)).status
    metrics: dict = {}
    for s in subsystems:
        for k, v in s.metrics.items():
            if k not in ("roles", "down") and v is not None:
                metrics[f"{s.name}.{k}"] = v
    return RuntimeHealthSnapshot(overall=overall, subsystems=subsystems, metrics=metrics)


def build_live_runtime_health() -> dict:
    """The live read-only snapshot as a bounded dict (HUD/CLI/voice safe)."""
    return collect_runtime_health().to_dict()


# ── live source readers (all guarded; any failure degrades to None, never raises) ──
def _live_fabric_metrics() -> dict:
    try:
        from core.collector_fabric import fabric
        return fabric.metrics()
    except Exception:  # noqa: BLE001
        return {}


def _live_telemetry() -> dict:
    try:
        from core.collector_fabric import fabric
        return fabric.telemetry_snapshot()
    except Exception:  # noqa: BLE001
        return {}


def _live_verifier() -> dict:
    try:
        from core.verification import verifier_latency_stats
        return verifier_latency_stats()
    except Exception:  # noqa: BLE001
        return {}


def _live_turn() -> dict:
    try:
        from core.turn_budget import turn_latency_stats
        return turn_latency_stats()
    except Exception:  # noqa: BLE001
        return {}


def _live_lifecycle() -> dict:
    try:
        from core.lifecycle import lifecycle
        return lifecycle.snapshot()
    except Exception:  # noqa: BLE001
        return {}


def _live_console() -> dict:
    try:
        from core.console import get_console
        c = get_console()
        return c.metrics() if c is not None else {}
    except Exception:  # noqa: BLE001
        return {}


def _live_watchdog_status() -> dict:
    try:
        from core import health_watchdog as hw
        out: dict = {}
        for name, info in getattr(hw, "_SUP", {}).items():
            task = info.get("task")
            out[name] = "running" if (task is not None and not task.done()) else "done"
        for name, st in hw._passive_status().items():
            out[name] = "running" if st.get("alive") else "done"
        return out
    except Exception:  # noqa: BLE001
        return {}


def _live_resource() -> dict | None:
    try:
        import psutil
        return {"cpu_percent": psutil.cpu_percent(interval=None),
                "ram_percent": psutil.virtual_memory().percent}
    except Exception:  # noqa: BLE001
        return None


def _live_profiler() -> dict:
    try:
        from core.performance_profiler import get_all_stats
        return get_all_stats()
    except Exception:  # noqa: BLE001
        return {}


def _live_model() -> dict:
    try:
        from core.ops_views import model_runtime_panel
        return model_runtime_panel()
    except Exception:  # noqa: BLE001
        return {}


def _live_spine() -> dict:
    out: dict = {}
    try:
        from core.correlation_v2 import correlator_v2
        out["correlation_findings"] = len(correlator_v2.recent(_MAX_LIST))
    except Exception:  # noqa: BLE001
        out["correlation_findings"] = 0
    try:
        from core.incident_workspace import workspace
        cases = workspace.open_cases()
        out["incidents_open"] = len(cases)
        verifs = [v for c in cases for v in getattr(c, "verification_results", [])]
        if verifs:
            ok = sum(1 for v in verifs if getattr(v, "verified", False))
            out["verification_success_rate"] = round(ok / len(verifs), 3)
    except Exception:  # noqa: BLE001
        out.setdefault("incidents_open", 0)
    return out
