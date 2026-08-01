"""training_gym/teachers/verifier_teacher.py — V69 M62: the local second opinion.

WHY THIS EXISTS
---------------
JARVIS already runs a VERIFIER-role model (``core.verification``) over its own drafts.
Reusing it as a teacher is the only review path that costs nothing, sends nothing and
is available offline — so it is worth having, and it is worth having on exactly the same
terms as every other provider.

THE SEAM, AND WHY IT IS INJECTED
--------------------------------
This module never imports the runtime, never constructs an LLM client and never resolves
a model alias by itself. It takes a ``verify_fn`` — a callable the caller wires from the
application — and when nobody wired one, the provider is simply unavailable.

That is deliberate and it is the security property, not a convenience. If this module
reached into ``core`` to build a client, then importing the gym would import the
runtime, a test would need the runtime, and "the verifier is unavailable" would become a
condition nobody could reproduce. Worse, a resolver that silently substituted a
different model would produce a review attributed to a model that never saw the packet.

WHAT IT WILL NOT DO
-------------------
No silent fallback to another model or another provider. No model download, ever. No
tool execution. No network beyond whatever the injected seam already does locally. No
dataset approval, no state mutation. When the runtime is absent the answer is
``SKIPPED``; when it answers badly the answer is ``ERROR``. Neither is ever a review.
"""
from __future__ import annotations

from collections.abc import Callable

from ..schemas import require_int
from .base import (
    MAX_TEACHER_RESPONSE_BYTES,
    ProviderResponse,
    ReviewMode,
    TeacherAvailability,
    TeacherError,
    TeacherKind,
    TeacherProvider,
    TeacherUnavailable,
)

#: The signature the injected seam must have: ``(prompt, timeout_s) -> str``.
#:
#: A plain string in, a plain string out. The provider does not hand the seam a packet
#: object, a trajectory or anything else it could reach further into, and it does not
#: accept a structured verdict back — everything a review says has to survive the same
#: strict JSON import every other provider's answer does.
VerifyFn = Callable[[str, int], str]

#: The local verifier is small and is running on a laptop beside everything else. A
#: review it cannot produce inside this budget is a review the operator does without.
DEFAULT_VERIFIER_TIMEOUT_S = 90


class VerifierTeacherProvider(TeacherProvider):
    """The local VERIFIER-role model, used as an offline second opinion."""

    provider_id = "verifier_local"
    provider_version = "m62.verifier.1"
    provider_kind = TeacherKind.LOCAL_VERIFIER
    is_cloud = False
    cost_bearing = False
    #: A local model is sampled; two runs may differ. Saying so matters, because a
    #: consumer that believed this was reproducible would treat one run as sufficient.
    deterministic = False
    supported_modes = (ReviewMode.LOCAL_LIVE,)

    def __init__(self, *, model: str, verify_fn: VerifyFn | None = None,
                 timeout_s: int = DEFAULT_VERIFIER_TIMEOUT_S,
                 unavailable_reason: str = "") -> None:
        if not str(model or "").strip():
            raise TeacherError(
                "VerifierTeacherProvider: an explicit resolved model id is required "
                "(never a role alias); a review attributed to whatever the alias "
                "pointed at that day is not reproducible")
        self._model = str(model).strip()
        self._verify_fn = verify_fn
        self._timeout_s = require_int(timeout_s, "verifier.timeout_s",
                                      minimum=1, maximum=300)
        self._unavailable_reason = str(unavailable_reason or "")

    @property
    def model(self) -> str:
        return self._model

    def availability(self) -> TeacherAvailability:
        """Honest about absence. Never probes, never installs, never downloads."""
        if self._unavailable_reason:
            return TeacherAvailability(available=False,
                                       reason=self._unavailable_reason)
        if self._verify_fn is None:
            return TeacherAvailability(
                available=False,
                reason="no local verifier runtime is wired into this gym; the gym "
                       "never starts one, never downloads a model and never falls back "
                       "to a different provider")
        if not callable(self._verify_fn):
            return TeacherAvailability(
                available=False,
                reason="the configured verifier seam is not callable")
        return TeacherAvailability(available=True, live_capable=True,
                                   reason="local verifier runtime is wired",
                                   detail={"model": self._model,
                                           "timeout_s": self._timeout_s})

    def _produce(self, packet: object, *, mode: ReviewMode,
                 timeout_s: int) -> ProviderResponse:
        if self._verify_fn is None:  # pragma: no cover — availability() gates this
            raise TeacherUnavailable("no local verifier runtime is wired")
        prompt = packet.to_prompt_text()  # type: ignore[attr-defined]
        budget = min(int(timeout_s), self._timeout_s)
        try:
            answer = self._verify_fn(prompt, budget)
        except TeacherError:
            raise
        except TimeoutError:
            raise TeacherUnavailable(
                f"local verifier did not answer within {budget}s") from None
        except Exception as exc:  # noqa: BLE001 — a crashed runtime produced nothing
            raise TeacherUnavailable(
                f"local verifier failed: {type(exc).__name__}") from None
        if not isinstance(answer, str):
            raise TeacherError(
                f"local verifier returned {type(answer).__name__}; a review must arrive "
                f"as text and pass the same strict JSON import as every other provider's")
        size = len(answer.encode("utf-8", "surrogatepass"))
        if size > MAX_TEACHER_RESPONSE_BYTES:
            raise TeacherError(f"local verifier returned {size} bytes, over the "
                               f"{MAX_TEACHER_RESPONSE_BYTES}-byte ceiling")
        return ProviderResponse(text=answer,
                                audit={"provider": self.provider_id,
                                       "model": self._model,
                                       "timeout_s": budget})


def verifier_from_seam(model: str, verify_fn: VerifyFn | None) -> VerifierTeacherProvider:
    """Build the provider from whatever the application wired, or a disabled one.

    A single constructor for both cases, so a caller cannot end up with two code paths
    where the "no runtime" branch quietly skips the provider entirely instead of
    recording that it was unavailable.
    """
    return VerifierTeacherProvider(model=model, verify_fn=verify_fn)


__all__ = ["DEFAULT_VERIFIER_TIMEOUT_S", "VerifierTeacherProvider", "VerifyFn",
           "verifier_from_seam"]
