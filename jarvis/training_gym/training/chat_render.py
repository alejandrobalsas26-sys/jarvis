"""training_gym/training/chat_render.py — V69 M62 S3M.1: how a chat template is CALLED.

WHY THIS MODULE EXISTS (defect D37)
-----------------------------------
``tokenizer_chat_template_hash`` digests the template **source**. Two subsystems can
therefore record the same digest and still hand the model different bytes, because a
chat template is a *function* and its arguments decide what it returns. That is exactly
what M62 did: the evaluation backend called ``apply_chat_template`` with an explicit
``enable_thinking`` taken from the plan-bound eligibility policy (``DISABLED``, operator
ruling H6a), while the training backend passed no such keyword and rendered under the
template's own default. Measured against the reviewed pinned Qwen3 template
(``a55ee1b1…``), those two calls differ by exactly ``'<think>\\n\\n</think>\\n\\n'``.

So the LoRA was fitted after one generation prefix and evaluated after another, and no
identity on either side could tell. This module is the single place that says what a
render call *means*, so both subsystems cannot drift again:

* :class:`ReasoningPolicy` — the closed set, shared. It lives here rather than in
  :mod:`training_gym.evaluation.generation` because the dependency runs
  evaluation → training and never the other way; ``DevicePolicy`` and
  ``PrecisionPolicy`` already sit on the training side for the same reason.
  ``evaluation.generation`` re-exports it, so every existing import still resolves and
  ``GenerationPolicy.policy_hash()`` is byte-identical — the member *values* did not move.
* :func:`reasoning_template_kwargs` — the one mapping from policy to kwargs.
* :func:`template_honours_reasoning_policy` — the one honour check. A template that
  ignores an unknown keyword renders the same string either way, and recording a setting
  as applied when it was not is worse than refusing.
* :class:`ChatRenderPolicy` — an identity over the *call*, not the source.

WHAT ``ChatRenderPolicy`` DELIBERATELY DOES NOT BIND
---------------------------------------------------
No cache path, no host name, no user name, no RAM figure, no timestamp, no output root.
It represents **model-facing semantics** and nothing else, so it is stable across roots
and hosts by construction. That is the D34/D36 rule — an identity that is a function of
incidental environment state is not an identity — applied before the field exists rather
than after a rebuild fails to reproduce.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..schemas import SchemaError, require_bool, require_enum, sha256_obj

#: Bumped when the *meaning* of a render policy changes. Part of every render digest, so
#: a digest taken under one interpretation can never silently equal one taken under
#: another.
CHAT_RENDER_POLICY_VERSION = "m62.chat_render_policy.1"


class ChatRenderError(SchemaError):
    """A render policy that would not describe what the model actually saw."""


class ReasoningPolicy(str, Enum):
    """Whether the chat template is asked to let the model think before answering.

    S3E.2 passed nothing, so the template's own default decided — and for a reasoning
    model that default is "think", which put a ``<think>`` block in front of every one
    of the 72 responses. That is a setting that changes every output, and a setting that
    changes every output belongs in the digest both arms are compared under, not in a
    kwarg a backend happens to omit.

    ``MODEL_DEFAULT`` is also the name for **what training historically did**. Training
    never bound a reasoning policy at all, so every configuration written before S3M.1
    reads back as ``MODEL_DEFAULT`` — which is the truth about those runs, and is
    deliberately *not* ``DISABLED``. Candidate 001 and candidate 002 were fitted under
    the template default and nothing here re-describes them.
    """

    #: Pass nothing; the chat template decides. What S3E.2 did, and what every training
    #: run up to and including candidate 002 did. The default, so no existing
    #: configuration silently changes behaviour or identity.
    MODEL_DEFAULT = "model_default"
    #: Ask the template for ``enable_thinking=False``.
    DISABLED = "disabled"
    #: Ask the template for ``enable_thinking=True``.
    ENABLED = "enabled"

    @property
    def template_kwarg(self) -> bool | None:
        """The value to pass, or ``None`` to pass nothing at all."""
        if self is ReasoningPolicy.MODEL_DEFAULT:
            return None
        return self is ReasoningPolicy.ENABLED

    @property
    def is_explicit(self) -> bool:
        """Whether this policy names a value rather than deferring to the template.

        The gate for every value-gated identity in this repository: a policy that defers
        renders exactly what it always rendered, so its record must hash exactly as it
        always hashed.
        """
        return self is not ReasoningPolicy.MODEL_DEFAULT


#: The reasoning policy a training run must bind to be rendered the way an
#: eligibility-grade evaluation renders it. It is the same value
#: ``training_gym.evaluation.generation.ELIGIBILITY_REASONING_POLICY`` carries, named on
#: the training side so a future training plan can be checked without importing the
#: evaluation package.
TRAIN_EVAL_PARITY_REASONING_POLICY = ReasoningPolicy.DISABLED


def reasoning_template_kwargs(policy: ReasoningPolicy) -> dict:
    """The keyword arguments this policy adds to an ``apply_chat_template`` call.

    One mapping, used by both subsystems. ``MODEL_DEFAULT`` adds nothing — passing
    ``enable_thinking=None`` is not the same call, because the reviewed template asks
    ``enable_thinking is defined``.
    """
    policy = require_enum(policy, ReasoningPolicy, "chat_render.reasoning_policy")
    value = policy.template_kwarg
    return {} if value is None else {"enable_thinking": value}


def template_honours_reasoning_policy(tokenizer, messages: list) -> bool:
    """Whether this chat template actually implements ``enable_thinking``.

    Rendered both ways and compared, rather than assumed. ``apply_chat_template`` passes
    an unknown keyword into the Jinja context and a template that never reads it renders
    the same string either way — which is a silent no-op, and a silent no-op in a record
    that says the setting was applied is worse than a refusal.
    """
    try:
        on = tokenizer.apply_chat_template(messages, tokenize=False,
                                           add_generation_prompt=True,
                                           enable_thinking=True)
        off = tokenizer.apply_chat_template(messages, tokenize=False,
                                            add_generation_prompt=True,
                                            enable_thinking=False)
    except Exception:  # noqa: BLE001 - a template that refuses the kwarg does not honour it
        return False
    return bool(on) and bool(off) and on != off


@dataclass(frozen=True)
class ChatRenderPolicy:
    """Everything about a chat-template CALL that can change the bytes the model sees.

    Two calls that agree on every field below produce the same rendering of the same
    logical messages. That is the property ``tokenizer_chat_template_hash`` cannot
    express, and it is the whole point of this record: it is what lets a test assert
    *"training rendered its prompt prefix exactly the way evaluation renders its prompt"*
    without either side having to publish the rendered text.

    ``add_generation_prompt`` is part of the identity because it is part of the call.
    A training run makes two calls with two different values — the prompt prefix
    (``True``) and the full supervised sequence (``False``) — and they are two different
    render policies with two different digests, correctly.
    """

    tokenizer_id: str
    tokenizer_revision: str
    chat_template_hash: str
    reasoning_policy: ReasoningPolicy = ReasoningPolicy.MODEL_DEFAULT
    add_generation_prompt: bool = True
    tokenize: bool = True
    policy_version: str = CHAT_RENDER_POLICY_VERSION

    def __post_init__(self) -> None:
        set_ = object.__setattr__
        for name in ("tokenizer_id", "tokenizer_revision", "chat_template_hash"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ChatRenderError(
                    f"chat_render.{name}: a render identity with an empty {name} "
                    f"describes no particular template, and two unrelated calls would "
                    f"share its digest")
            set_(self, name, value.strip())
        set_(self, "reasoning_policy",
             require_enum(self.reasoning_policy, ReasoningPolicy,
                          "chat_render.reasoning_policy"))
        set_(self, "add_generation_prompt",
             require_bool(self.add_generation_prompt,
                          "chat_render.add_generation_prompt"))
        set_(self, "tokenize", require_bool(self.tokenize, "chat_render.tokenize"))

    @property
    def template_kwargs(self) -> dict:
        return reasoning_template_kwargs(self.reasoning_policy)

    def to_dict(self) -> dict:
        """Structured semantics, never a sample rendering.

        ``enable_thinking`` is carried alongside ``reasoning_policy`` on purpose: the
        policy is this repository's vocabulary and the keyword is the library's, and a
        reader of an artefact should not have to know the mapping to check the call.
        ``null`` means "the keyword was not passed", which is a different call from
        passing ``false``.
        """
        return {
            "policy_version": self.policy_version,
            "tokenizer_id": self.tokenizer_id,
            "tokenizer_revision": self.tokenizer_revision,
            "chat_template_hash": self.chat_template_hash,
            "reasoning_policy": self.reasoning_policy.value,
            "enable_thinking": self.reasoning_policy.template_kwarg,
            "add_generation_prompt": self.add_generation_prompt,
            "tokenize": self.tokenize,
        }

    def render_policy_hash(self) -> str:
        """SHA-256 over the canonical form. No host state is reachable from here."""
        return sha256_obj(self.to_dict())


__all__ = [
    "CHAT_RENDER_POLICY_VERSION", "ChatRenderError", "ChatRenderPolicy",
    "ReasoningPolicy", "TRAIN_EVAL_PARITY_REASONING_POLICY",
    "reasoning_template_kwargs", "template_honours_reasoning_policy",
]
