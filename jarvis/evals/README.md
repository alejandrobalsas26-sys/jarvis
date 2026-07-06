# JARVIS Evaluation Datasets (V64 M14)

Versioned `EvalCase` datasets (JSONL, one case per line) consumed by
`core.eval_harness`. Each case is scored **deterministically** wherever possible;
model-graded checks run only when a case carries a `rubric`.

## Layout

```
evals/
  prompt_injection/   injection-resistance adversarial set (targets M12 firewall)
  sql_injection/      SQLi detection set (targets M13 security analyzer)
  general/ research/ coding/ ...   (domain suites, added as targets mature)
```

## Case schema (`EvalCase.to_dict`)

```jsonc
{
  "id": "inj-web-tool-call",
  "domain": "prompt_injection",
  "prompt": "human-readable description",
  "expect": {                        // only set dimensions are scored
    "contains": ["..."],             // substrings the answer must include
    "not_contains": ["..."],         // substrings it must NOT include
    "required_tools": ["..."],       // tools that must be used
    "forbidden_tools": ["..."],      // tools that must NOT be used
    "domain": "coder",               // expected TaskDomain routing
    "min_confidence": 0.6,
    "verified": true,
    "injection_detected": true,
    "injection_quarantined": true,
    "must_cite": true,               // every citation must be validly fetched
    "max_latency_s": 5.0
  },
  "context": { "origin": "web_untrusted", "content": "..." },
  "ground_truth": "optional expected fact",
  "rubric": "optional model-graded rubric",
  "tags": ["injection", "web"],
  "timeout_s": 10.0
}
```

## Running a suite

```python
import asyncio
from core.eval_harness import EvalRunner, load_cases, firewall_eval_target, compare_runs

cases = load_cases("evals/prompt_injection/injection_resistance.jsonl")
runner = EvalRunner(firewall_eval_target())
run = asyncio.run(runner.run_suite(cases, run_id="firewall-baseline"))
print(run.summary())          # pass_rate, per-metric, per-domain
run.save("evals/_results/firewall-baseline.jsonl")

# Later, gate a change on regressions:
# report = compare_runs(baseline_run, candidate_run)
# assert not report.has_regression
```

Results are **never** vague "looks better" — `compare_runs` reports per-metric
and pass-rate deltas so a model/change is only promoted when it does not regress.
