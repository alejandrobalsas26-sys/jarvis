"""scripts/qualify_reasoning_policy.py — V69 M62 S3F.2: does the template honour the ruling?

WHAT THIS ANSWERS AND WHAT IT REFUSES TO ANSWER
------------------------------------------------
Operator ruling **H6a** approved ``reasoning_policy = DISABLED`` for a future
eligibility-grade evaluation. That ruling is only meaningful if the reviewed tokenizer's
chat template actually implements ``enable_thinking``. ``apply_chat_template`` passes an
unknown keyword straight into the Jinja context, so a template that never reads it renders
identically either way — a silent no-op, under a setting the report would record as
applied. This script renders the same prompt both ways and compares them.

**It generates nothing.** No model is loaded, no weights are read, no token is produced,
no plan is consumed. Rendering a chat template is string formatting; it is the one thing
the offline invariants permit and the only thing this does.

FAIL CLOSED, IN BOTH DIRECTIONS
-------------------------------
* the reviewed cache cannot be located -> ``blocked_cache_not_located``;
* transformers is not importable -> ``blocked_runtime_unavailable``;
* the two renderings are identical, or the template raises on the keyword ->
  ``chat_template_does_not_honour_reasoning_policy``.

Only two different, non-empty renderings produce ``pass``. Nothing here weakens into
"probably fine".

OFFLINE, ALWAYS
---------------
``HF_HUB_OFFLINE=1``, ``TRANSFORMERS_OFFLINE=1``, ``HF_HUB_DISABLE_TELEMETRY=1`` are set
before transformers is imported, and the load is ``local_files_only=True``,
``trust_remote_code=False``, pinned to an immutable revision, bound to the reviewed
``cache_dir``. It never guesses a cache location: an unnamed root is ``blocked``, because
guessing one and finding it empty is indistinguishable from having looked in the wrong
place.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:  # pragma: no cover - layout shim, as the sibling CLIs do
    sys.path.insert(0, str(_ROOT))

#: The reviewed model of record for V69 M62. Pinned to an immutable commit, never a
#: branch or a tag: a tag can move and a moved tag silently re-points every measurement.
MODEL_ID = "Qwen/Qwen3-0.6B"
MODEL_REVISION = "c1899de289a04d12100db370d81485cdf75e47ca"

#: A neutral probe prompt. It carries no task material, no evidence and no hidden target,
#: because what is being measured is the template, not the model.
PROBE_MESSAGES = [{"role": "user", "content": "Summarise the supplied alert."}]

EXIT_PASS = 0
EXIT_BLOCKED = 3
EXIT_NOT_HONOURED = 4


def cache_candidates(explicit: str | None) -> list[Path]:
    """Roots that may hold the reviewed cache, most authoritative first.

    An explicit ``--model-cache-root`` wins. Otherwise the environment variables the
    Hugging Face libraries themselves read, and then the single documented default. This
    list is deliberately short and never walks the filesystem looking for weights.
    """
    if explicit:
        return [Path(explicit)]
    roots: list[Path] = []
    for variable in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE", "TRANSFORMERS_CACHE"):
        value = os.environ.get(variable)
        if value:
            roots.append(Path(value))
    home = os.environ.get("HF_HOME")
    if home:
        roots.append(Path(home) / "hub")
    roots.append(Path.home() / ".cache" / "huggingface" / "hub")
    return roots


def locate_cache(explicit: str | None) -> tuple[Path | None, str]:
    """The first candidate that actually holds this model, and how it was found.

    Uses the repository's own ``probe_cache`` authority rather than a second opinion about
    what "cached" means, and reports the cache root's digest rather than its path — enough
    to prove two probes saw the same thing, not enough to publish where an operator keeps
    their models.
    """
    from training_gym.schemas import sha256_text
    from training_gym.training.model_identity import CacheStatus, probe_cache

    for root in cache_candidates(explicit):
        status, _evidence = probe_cache(MODEL_ID, cache_root=root)
        if status is CacheStatus.PRESENT:
            return root, sha256_text(str(root))[:16]
    return None, ""


def qualify(*, cache_root: str | None = None) -> dict:
    """Render the probe prompt under both reasoning policies and compare. No generation."""
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

    from training_gym.evaluation.generation import (
        ELIGIBILITY_REASONING_POLICY,
        eligibility_generation_policy,
    )

    policy = eligibility_generation_policy(seed=11)
    base = {
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "reasoning_policy": ELIGIBILITY_REASONING_POLICY.value,
        "policy_hash": policy.policy_hash(),
        "generation_performed": False,
        "tokens_produced": 0,
    }

    root, evidence = locate_cache(cache_root)
    if root is None:
        return {**base, "preflight": "blocked_cache_not_located",
                "detail": "no candidate cache root holds the reviewed model; name one "
                          "with --model-cache-root. A guessed root that turns out empty "
                          "is indistinguishable from having looked in the wrong place"}
    base["cache_root_evidence"] = evidence

    try:
        from transformers import AutoTokenizer
    except Exception as exc:  # noqa: BLE001 - an absent optional package is a refusal
        return {**base, "preflight": "blocked_runtime_unavailable",
                "detail": f"{type(exc).__name__}: transformers is not importable in this "
                          f"interpreter"}
    try:
        tokenizer = AutoTokenizer.from_pretrained(
            MODEL_ID, revision=MODEL_REVISION, cache_dir=str(root),
            local_files_only=True, trust_remote_code=False)
    except Exception as exc:  # noqa: BLE001 - an unreadable cache proves nothing
        return {**base, "preflight": "blocked_cache_not_readable",
                "detail": f"{type(exc).__name__}: the reviewed tokenizer did not load "
                          f"offline from the located cache"}

    from training_gym.evaluation.backends.transformers_peft import (
        template_honours_reasoning_policy,
    )
    honoured = template_honours_reasoning_policy(tokenizer, PROBE_MESSAGES)
    rendered = {}
    for label, value in (("model_default", None), ("disabled", False),
                         ("enabled", True)):
        kwargs = {} if value is None else {"enable_thinking": value}
        try:
            text = tokenizer.apply_chat_template(
                PROBE_MESSAGES, tokenize=False, add_generation_prompt=True, **kwargs)
        except Exception as exc:  # noqa: BLE001
            rendered[label] = {"error": type(exc).__name__}
            continue
        # Lengths and a digest, never the rendered string: a chat template can carry a
        # system preamble, and a preamble is content this file has no reason to publish.
        from training_gym.schemas import sha256_text
        rendered[label] = {"chars": len(text), "sha256": sha256_text(text)[:16]}

    if not honoured:
        return {**base, "renderings": rendered,
                "preflight": "chat_template_does_not_honour_reasoning_policy",
                "detail": "the template renders identically with enable_thinking on and "
                          "off, or refuses the keyword. Measuring under settings the "
                          "report says were applied, but were not, is the failure this "
                          "refuses"}
    return {**base, "renderings": rendered, "preflight": "pass",
            "detail": "the reviewed chat template renders differently under "
                      "enable_thinking on and off, so reasoning_policy=DISABLED is a "
                      "request the template implements"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline preflight: does the reviewed chat template honour "
                    "reasoning_policy? Renders a prompt; generates nothing.")
    parser.add_argument("--model-cache-root", default=None,
                        help="the reviewed local Hugging Face cache root")
    args = parser.parse_args(argv)
    report = qualify(cache_root=args.model_cache_root)
    print(json.dumps(report, indent=2, sort_keys=True))
    if report["preflight"] == "pass":
        return EXIT_PASS
    if report["preflight"].startswith("blocked"):
        return EXIT_BLOCKED
    return EXIT_NOT_HONOURED


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
