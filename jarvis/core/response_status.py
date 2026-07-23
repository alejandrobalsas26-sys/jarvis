"""core/response_status.py — V69 M57.9: read-only response-pipeline panels.

Renders the ``/response-status`` | ``/response-profile`` | ``/latency`` |
``/context-status`` | ``/tts-status`` views from state the runtime already holds.

Every panel is READ-ONLY and CONTENT-FREE: enums, counters, timings and budgets.
No prompt, no answer text, no memory record, no secret, no token and no internal
HTTP object ever appears here — same rule the runtime-health snapshot follows.

Each reader degrades to a truthful "n/a" rather than guessing, and no panel is ever
printed at boot: they exist for an operator who asks.
"""
from __future__ import annotations

_NA = "n/a"


def _fmt(value, *, suffix: str = "", digits: int = 1) -> str:
    if value is None:
        return _NA
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.{digits}f}{suffix}"
    return f"{value}{suffix}"


def _runtime():
    try:
        from core.response_runtime import get_response_runtime
        return get_response_runtime()
    except Exception:  # noqa: BLE001
        return None


def _settings():
    try:
        from core.config import settings
        return settings
    except Exception:  # noqa: BLE001
        return None


def render_response_status() -> str:
    """The main panel: what shaped the LAST turn and what it cost."""
    rr = _runtime()
    s = _settings()
    lines = ["RESPONSE PIPELINE"]
    if rr is None:
        return "RESPONSE PIPELINE\n  unavailable"
    handle = rr.current
    recent = rr.recent()
    last = handle.snapshot() if handle is not None else (recent[-1] if recent else {})
    tp = rr.throughput.snapshot()
    lines += [
        f"  contract={last.get('contract') or _NA}",
        f"  reason={last.get('selection_reason') or _NA}",
        f"  language={last.get('language') or _NA}",
        f"  profile={rr.profile.value}",
        f"  token_budget={_fmt(last.get('token_budget') or None)}",
        f"  context_budget={_fmt(last.get('context_budget') or None)}",
        "  transport=native think=false"
        if s is not None and getattr(s, "fast_transport", "auto") != "openai"
        else "  transport=openai",
        f"  first_fragment={_fmt(last.get('first_fragment_ms'), suffix='ms')}",
        f"  first_sentence={_fmt(last.get('first_sentence_ms'), suffix='ms')}",
        f"  throughput={_fmt(tp.get('median_tok_s'), suffix=' tok/s')} "
        f"(n={tp.get('samples', 0)})",
        f"  terminal_state={last.get('state') or _NA}",
        f"  truncated={_fmt(last.get('truncated_by_cap'))}",
        f"  continuation={_fmt(last.get('continuation_available'))}",
        f"  tts={'muted' if rr.muted else 'progressive'}",
    ]
    return "\n".join(lines)


def render_response_profile() -> str:
    """The session verbosity profile and the bounds it operates inside."""
    rr = _runtime()
    s = _settings()
    if rr is None:
        return "RESPONSE PROFILE\n  unavailable"
    return "\n".join([
        "RESPONSE PROFILE",
        f"  profile={rr.profile.value}",
        f"  contracts_enabled={_fmt(getattr(s, 'response_contracts_enabled', True))}",
        f"  adaptive_budget={_fmt(getattr(s, 'response_adaptive_budget', True))}",
        f"  max_output_tokens={_fmt(getattr(s, 'response_max_output_tokens', None))}",
        f"  fast_context={_fmt(getattr(s, 'fast_context', None))}",
        "  commands=/brief /standard /detailed /auto",
    ])


def render_latency() -> str:
    """Measured turn latency and the throughput the budgets are derived from."""
    rr = _runtime()
    lines = ["RESPONSE LATENCY"]
    if rr is not None:
        tp = rr.throughput.snapshot()
        lines += [
            f"  throughput={_fmt(tp.get('median_tok_s'), suffix=' tok/s')}",
            f"  samples={tp.get('samples', 0)} rejected={tp.get('rejected_samples', 0)}",
            f"  first_token={_fmt(tp.get('median_first_token_ms'), suffix='ms')}",
            f"  turns={rr.turns_started} completed={rr.turns_completed}",
            f"  interrupted={rr.interrupted_turns} replaced={rr.replaced_turns} "
            f"timed_out={rr.timed_out_turns}",
            f"  cancellation_latency={_fmt(rr.cancellation_latency_ms, suffix='ms')}",
        ]
    try:
        from core.turn_budget import turn_latency_stats
        st = turn_latency_stats()
        lines += [
            f"  avg_total={_fmt(st.get('avg_total_ms'), suffix='ms')}",
            f"  max_total={_fmt(st.get('max_total_ms'), suffix='ms')}",
            f"  last_timeout_stage={st.get('last_timeout_stage') or _NA}",
        ]
    except Exception:  # noqa: BLE001
        lines.append("  turn stats unavailable")
    return "\n".join(lines)


def render_context_status() -> str:
    """The bounded live-prompt composition of the last turn (M57.6)."""
    s = _settings()
    lines = ["CONTEXT"]
    try:
        from core.context_composer import last_context_metrics
        m = last_context_metrics()
    except Exception:  # noqa: BLE001
        m = {}
    if not m:
        lines.append("  no composed turn yet")
    else:
        lines += [
            f"  estimated_total_tokens={_fmt(m.get('estimated_total_tokens'))}",
            f"  budget={_fmt(m.get('token_budget'))}",
            f"  system={_fmt(m.get('system_tokens'))} "
            f"digest={_fmt(m.get('digest_tokens'))} "
            f"recent={_fmt(m.get('recent_turn_tokens'))}",
            f"  tool_evidence={_fmt(m.get('tool_evidence_tokens'))} "
            f"memory_evidence={_fmt(m.get('memory_evidence_tokens'))}",
            f"  trimmed_items={_fmt(m.get('trimmed_items'))} "
            f"digest_age_turns={_fmt(m.get('digest_age_turns'))}",
        ]
    lines.append(f"  fast_context={_fmt(getattr(s, 'fast_context', None))}")
    return "\n".join(lines)


def render_tts_status() -> str:
    """Progressive-speech state, queue depth and backpressure counters."""
    rr = _runtime()
    lines = ["SPEECH"]
    lines.append(f"  muted={_fmt(bool(getattr(rr, 'muted', False)))}")
    try:
        from core.speech_stream import last_speech_metrics
        m = last_speech_metrics()
    except Exception:  # noqa: BLE001
        m = {}
    if not m:
        lines.append("  no spoken turn yet")
        return "\n".join(lines)
    lines += [
        f"  progressive={_fmt(m.get('progressive_enabled'))}",
        f"  first_utterance={_fmt(m.get('first_utterance_ms'), suffix='ms')}",
        f"  queued={_fmt(m.get('queued'))} split={_fmt(m.get('split_utterances'))}",
        f"  duplicates_suppressed={_fmt(m.get('suppressed_duplicates'))} "
        f"stale_dropped={_fmt(m.get('stale_dropped'))}",
        f"  code_skipped={_fmt(m.get('code_skipped'))} "
        f"muted_skipped={_fmt(m.get('muted_skipped'))}",
        f"  depth={_fmt(m.get('current_depth'))} "
        f"high_watermark={_fmt(m.get('high_watermark'))}",
    ]
    return "\n".join(lines)


_PANELS = {
    "RESPONSE_STATUS": render_response_status,
    "RESPONSE_PROFILE": render_response_profile,
    "LATENCY": render_latency,
    "CONTEXT_STATUS": render_context_status,
    "TTS_STATUS": render_tts_status,
}


def render_panel(command) -> str:
    """Render the panel for a :class:`~core.response_commands.ResponseCommand`."""
    key = getattr(command, "value", str(command))
    fn = _PANELS.get(key)
    return fn() if fn is not None else f"unknown panel: {key}"
