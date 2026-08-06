"""training_gym/training/model_identity.py — V69 M62 S3A: which weights, exactly.

WHY A REVISION IS NOT A DETAIL
------------------------------
``Qwen/Qwen3-0.6B`` names a repository, not a model. The bytes behind that name change
whenever the upstream author pushes, and a plan whose base weights can change underneath
it is not reproducible — the same argument :func:`training_gym.sandbox.security.
validate_image` already makes about container tags, applied to the other thing this
system pins.

So a model reference is executable only when it carries a full 40-character commit sha.
A branch name is accepted as *well-formed* and reported as *not executable*: the template
an operator starts from has to load before it can be told what is missing.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not resolve a revision, contact a hub, read a token, import ``transformers`` or
``huggingface_hub``, or instantiate a tokenizer. Cache presence is inspected by looking
for a directory *name* under an explicitly supplied root — never by asking a library,
because every library that answers that question also knows how to download the answer.
The absolute cache path never leaves this module: only a boolean and a digest do.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from ..schemas import SchemaError, sha256_obj, sha256_text
from .config import (
    TrainingConfig,
    is_immutable_revision,
    require_model_id,
    require_revision,
)
from .dataset_reference import is_placeholder

#: Bumped when the identity record's shape changes. Part of the plan hash.
MODEL_IDENTITY_VERSION = "m62.model_identity.1"

#: Versioned separately from :data:`MODEL_IDENTITY_VERSION` so the two digests can never
#: be confused for one another, and so this one can be corrected without invalidating a
#: single already-recorded manifest. See :func:`canonical_identity_fields`.
CANONICAL_MODEL_IDENTITY_VERSION = "m62.model_identity.canonical.1"


class ModelIdentityError(SchemaError):
    """A model or tokenizer reference that will not be used as written."""


class CacheStatus(str, Enum):
    """What is known about local weights. ``UNKNOWN`` is a real answer, not a failure.

    A probe that crashed proved nothing, and reporting that as ``ABSENT`` would let a
    planner claim "a download will be required" when it has no idea, or — far worse —
    claim ``PRESENT`` and let a later stage skip the check.
    """

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class RevisionKind(str, Enum):
    IMMUTABLE_COMMIT = "immutable_commit"
    MUTABLE_REFERENCE = "mutable_reference"
    PLACEHOLDER = "placeholder"


def classify_revision(revision: str) -> RevisionKind:
    """Immutable, mutable, or an unfilled template slot."""
    if is_placeholder(revision):
        return RevisionKind.PLACEHOLDER
    if is_immutable_revision(revision):
        return RevisionKind.IMMUTABLE_COMMIT
    return RevisionKind.MUTABLE_REFERENCE


#: Hugging Face lays weights out as ``models--<org>--<name>`` under its hub cache.
def cache_directory_name(model_id: str) -> str:
    """The directory name a hub cache would use for this model. A name, not a path."""
    org, name = require_model_id(model_id, "model_id").split("/", 1)
    return f"models--{org}--{name}"


def canonical_identity_fields(*, model_id: str, revision: str,
                              tokenizer_id: str = "", tokenizer_revision: str = "",
                              requires_remote_code: bool = False) -> dict:
    """The durable identity: which bytes load, and nothing else.

    WHY THIS EXISTS SEPARATELY FROM :meth:`ModelIdentity.identity_hash`
    ------------------------------------------------------------------
    ``identity_hash`` covers the whole record, including ``cache_status``,
    ``cache_evidence`` and ``license_reference``. None of those three change which
    weights load. Two of them are *host state* and one is *documentation*, so the same
    model bytes hash differently depending on where the operator keeps their cache and
    what sentence they wrote about the licence — and a pairing check built on that
    refuses a legitimate comparison while proving nothing about the weights.

    This record binds only what an execution would actually resolve: the repository, the
    immutable commit behind it, the tokenizer half, and the remote-code policy. The
    commit sha *is* the architecture identity — it pins ``config.json`` — so a declared
    ``parameters_b`` or ``family`` adds an operator's description of the model, not a
    fact about it, and is deliberately excluded for the same reason as the licence text.
    """
    mid = require_model_id(model_id, "canonical_identity.model_id")
    rev = require_revision(revision, "canonical_identity.revision")
    tid = require_model_id(tokenizer_id or mid, "canonical_identity.tokenizer_id")
    trev = require_revision(tokenizer_revision or rev,
                            "canonical_identity.tokenizer_revision")
    if tid != mid:
        raise ModelIdentityError(
            f"canonical_identity.tokenizer_id: {tid!r} is not the model {mid!r}; a "
            f"substituted tokenizer changes what every token id means")
    if trev != rev:
        raise ModelIdentityError(
            "canonical_identity.tokenizer_revision: must equal the model revision")
    if requires_remote_code:
        raise ModelIdentityError(
            "canonical_identity.requires_remote_code: a model that executes Python from "
            "its own repository has no reviewed path in this milestone")
    return {
        "canonical_version": CANONICAL_MODEL_IDENTITY_VERSION,
        "model_id": mid,
        "revision": rev,
        "revision_kind": classify_revision(rev).value,
        "tokenizer_id": tid,
        "tokenizer_revision": trev,
        "requires_remote_code": False,
    }


def canonical_identity_hash(*, model_id: str, revision: str, tokenizer_id: str = "",
                            tokenizer_revision: str = "",
                            requires_remote_code: bool = False) -> str:
    """Digest of :func:`canonical_identity_fields`.

    This is the compatibility bridge: an adapter manifest recorded before this authority
    existed still names its base model, revision and tokenizer, so its durable identity
    can be re-derived from what it already wrote down. Nothing is rewritten.
    """
    return sha256_obj(canonical_identity_fields(
        model_id=model_id, revision=revision, tokenizer_id=tokenizer_id,
        tokenizer_revision=tokenizer_revision,
        requires_remote_code=requires_remote_code))


@dataclass(frozen=True)
class ModelIdentity:
    """Everything the plan knows about the weights, and nothing about the filesystem."""

    provider: str
    model_id: str
    revision: str
    parameters_b: float
    family: str = ""
    tokenizer_id: str = ""
    tokenizer_revision: str = ""
    requires_remote_code: bool = False
    cache_status: CacheStatus = CacheStatus.UNKNOWN
    cache_evidence: str = ""
    license_reference: str = ""
    identity_version: str = MODEL_IDENTITY_VERSION

    def __post_init__(self) -> None:
        setattr_ = object.__setattr__
        setattr_(self, "model_id", require_model_id(self.model_id,
                                                    "model_identity.model_id"))
        setattr_(self, "revision", require_revision(self.revision,
                                                    "model_identity.revision"))
        setattr_(self, "tokenizer_id",
                 require_model_id(self.tokenizer_id or self.model_id,
                                  "model_identity.tokenizer_id"))
        setattr_(self, "tokenizer_revision",
                 require_revision(self.tokenizer_revision or self.revision,
                                  "model_identity.tokenizer_revision"))
        if self.tokenizer_id != self.model_id:
            raise ModelIdentityError(
                f"model_identity.tokenizer_id: {self.tokenizer_id!r} is not the model "
                f"{self.model_id!r}; a substituted tokenizer changes what every token id "
                f"means and produces no error anywhere")
        if self.tokenizer_revision != self.revision:
            raise ModelIdentityError(
                "model_identity.tokenizer_revision: must equal the model revision")
        if self.requires_remote_code:
            raise ModelIdentityError(
                "model_identity.requires_remote_code: a model that requires executing "
                "Python from its repository is refused; this milestone has no reviewed "
                "path for that and no configuration may create one")
        if not isinstance(self.parameters_b, (int, float)) or isinstance(
                self.parameters_b, bool):
            raise ModelIdentityError("model_identity.parameters_b: expected a number")
        if not (0 < float(self.parameters_b) < 1e4):
            raise ModelIdentityError(
                f"model_identity.parameters_b: {self.parameters_b} outside (0, 1e4)")
        setattr_(self, "parameters_b", float(self.parameters_b))
        if self.cache_status not in tuple(CacheStatus):
            setattr_(self, "cache_status", CacheStatus(self.cache_status))

    @property
    def revision_kind(self) -> RevisionKind:
        return classify_revision(self.revision)

    @property
    def is_executable_reference(self) -> bool:
        """True only when the weights this names cannot change underneath a run."""
        return self.revision_kind is RevisionKind.IMMUTABLE_COMMIT

    @property
    def blockers(self) -> tuple[str, ...]:
        """Why this identity is not yet executable. Empty means it is."""
        kind = self.revision_kind
        if kind is RevisionKind.PLACEHOLDER:
            return (f"{self.model_id}: the revision is still the template placeholder "
                    f"{self.revision!r}; replace it with the 40-character commit sha of "
                    f"the exact snapshot to train against",)
        if kind is RevisionKind.MUTABLE_REFERENCE:
            return (f"{self.model_id}: revision {self.revision!r} is a mutable reference; "
                    f"the weights behind it change whenever the upstream repository is "
                    f"updated, so a plan pinned to it is not reproducible",)
        return ()

    def to_dict(self) -> dict:
        """The exportable record. Carries no path — only a status and a digest."""
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "revision": self.revision,
            "revision_kind": self.revision_kind.value,
            "parameters_b": self.parameters_b,
            "family": self.family,
            "tokenizer_id": self.tokenizer_id,
            "tokenizer_revision": self.tokenizer_revision,
            "requires_remote_code": self.requires_remote_code,
            "cache_status": self.cache_status.value,
            "cache_evidence": self.cache_evidence,
            "license_reference": self.license_reference,
            "identity_version": self.identity_version,
        }

    def identity_hash(self) -> str:
        """The legacy whole-record digest. Unchanged, and deliberately so.

        Every adapter manifest already on disk recorded this value. Changing what it
        covers would silently invalidate them all, so the correction is additive:
        :meth:`canonical_identity_hash` is the digest new pairing checks use.
        """
        return sha256_obj(self.to_dict())

    def canonical_identity(self) -> dict:
        """The durable byte identity. Annotations and host state are excluded."""
        return canonical_identity_fields(
            model_id=self.model_id, revision=self.revision,
            tokenizer_id=self.tokenizer_id,
            tokenizer_revision=self.tokenizer_revision,
            requires_remote_code=self.requires_remote_code)

    def canonical_identity_hash(self) -> str:
        """Stable across a cache probe and across any licence wording."""
        return sha256_obj(self.canonical_identity())

    def tokenizer_identity_hash(self) -> str:
        """A separate digest for the tokenizer half, so a plan can bind both."""
        return sha256_obj({"tokenizer_id": self.tokenizer_id,
                           "tokenizer_revision": self.tokenizer_revision,
                           "requires_remote_code": self.requires_remote_code,
                           "identity_version": self.identity_version})


def probe_cache(model_id: str, *, cache_root: str | Path | None) -> tuple[CacheStatus, str]:
    """Look for locally cached weights under an explicitly authorised root.

    Returns ``(status, evidence)`` where evidence is a digest of the directory *name*
    that was found — enough to prove two probes saw the same thing, and not enough to
    tell a reader where the operator keeps their models. No root means ``UNKNOWN``: this
    function never guesses a default cache location, because guessing one and finding it
    absent is indistinguishable from having looked in the wrong place.
    """
    if cache_root is None:
        return CacheStatus.UNKNOWN, ""
    try:
        root = Path(cache_root)
        if not root.is_dir():
            return CacheStatus.UNKNOWN, ""
        entry = root / cache_directory_name(model_id)
        if entry.is_symlink():
            return CacheStatus.UNKNOWN, ""
        if entry.is_dir() and any(entry.iterdir()):
            return CacheStatus.PRESENT, sha256_text(entry.name)
        return CacheStatus.ABSENT, ""
    except (OSError, SchemaError, ModelIdentityError):
        # A probe that failed proved nothing; saying ABSENT here would let a caller
        # conclude "a download is needed" from an unreadable directory.
        return CacheStatus.UNKNOWN, ""


def identity_from_config(config: TrainingConfig, *,
                         cache_root: str | Path | None = None,
                         provider: str = "huggingface") -> ModelIdentity:
    """Derive the identity a config declares. Reads nothing but the config and the cache."""
    status, evidence = probe_cache(config.base_model_id, cache_root=cache_root)
    return ModelIdentity(
        provider=provider,
        model_id=config.base_model_id,
        revision=config.base_model_revision,
        parameters_b=config.base_model_parameters_b,
        family=config.base_model_family,
        tokenizer_id=config.tokenizer_id,
        tokenizer_revision=config.tokenizer_revision,
        requires_remote_code=config.trust_remote_code,
        cache_status=status,
        cache_evidence=evidence)


__all__ = [
    "CANONICAL_MODEL_IDENTITY_VERSION", "MODEL_IDENTITY_VERSION", "CacheStatus",
    "ModelIdentity", "ModelIdentityError", "RevisionKind", "cache_directory_name",
    "canonical_identity_fields", "canonical_identity_hash", "classify_revision",
    "identity_from_config", "probe_cache",
]
