# V69 M68D — the CHECK/EFFECT invariant

A short architecture note. It exists because five independently reported
production defects turned out to be one shape, and naming the shape is cheaper
than rediscovering it a sixth time.

## The invariant

```
VALIDATION MUST BIND TO THE ACTUAL EFFECT, BYTES, DESTINATION OR
PLATFORM STATE THAT JARVIS LATER CLAIMS IT USED.
```

A control is only worth the thing it is bound to. Every one of the five M68D
findings was a control that ran correctly and then answered about a *different
reality* than the one the system went on to act in and report on.

| | the check asked about | the effect happened to | measured consequence |
|---|---|---|---|
| H01 | DNS answer #1 | DNS answer #2 | policy approved `93.184.216.34`, the socket reached `127.0.0.1`, and the loopback body came back with `error = None` |
| H02 | a later reopening of the path | the bytes already read | content v1 returned with `sha256(v2)` and `complete: true`; a CAS write then destroyed an unread human edit |
| H03 | the result, after the summary | the summary, built from raw | synthetic password and bearer canaries reached the audit JSONL and the AURA payload |
| H04 | nothing (return codes discarded) | two firewall rules | all three release outcomes returned the identical `{"released": True}` and erased local tracking |
| H05 | the host filesystem | the repository | 131 false "corruption" problems on an `autocrlf=true` checkout; `os.access(X_OK)` wrong in both directions |

## Why each one is the same bug

The pattern is a **temporal or representational gap between the check and the
effect**, with no identity carried across it:

- **H01** — a gap in *time*. Two resolutions of one name, nothing pinning the
  second to the first. Closed by making the decision carry the address
  (`EgressDestination.pinned`) and making the transport consume it.
- **H02** — a gap in *time*. Two openings of one path. Closed by holding ONE
  descriptor (`source_snapshot`) so the digest and the text cannot describe
  different bytes.
- **H03** — a gap in *order*. The approved representation was computed after the
  emitted one. Closed by sanitizing before summarising, at the sinks.
- **H04** — a gap in *evidence*. The check never happened: return codes were
  discarded and the result asserted. Closed by per-rule evidence and by
  *querying* the firewall instead of trusting an exit code.
- **H05** — a gap in *representation*. The check and the effect used different
  vocabularies for the same object (native vs repository paths, filesystem vs
  Git modes, converted vs sealed bytes). Closed by speaking the repository's
  vocabulary, and by pinning the bytes rather than normalising them.

## The three questions this note is for

When adding or reviewing a control:

1. **What exactly did the check observe?** Name the observation — a DNS answer, a
   descriptor, a string, a return code — not the abstraction.
2. **Is that the same observation the effect will use?** If the effect re-derives
   it (resolves again, reopens, re-serialises, re-stats), the control is bound to
   nothing.
3. **If the two can differ, which one does the receipt describe?** A receipt that
   describes the check while the effect used something else is the failure mode
   all five findings share.

## What this note is NOT

It is not a mandate to rewrite anything. Each M68D fix is local to the surface it
belongs to, and the five surfaces share no new abstraction — deliberately. There
is no `CheckEffectBinding` base class and there should not be one: the binding is
different in each case (an IP, a file descriptor, an ordering, a query, a path
vocabulary), and a generic wrapper would hide exactly the detail that matters.

## Related

- `docs/v69_M68D_PRODUCTION_TRUST_BOUNDARY_HARDENING.md` — the findings, their
  reproductions and their residual limitations.
- M65D's `ExternalOutcome` — the same refusal to round an unknown into a
  certainty, which H04 reuses per rule rather than reinventing.
- M68C's `SourceIdentity` — the rendering/identity split H02 extends from one
  file to one *observation*.
