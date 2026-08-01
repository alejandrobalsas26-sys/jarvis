"""training_gym/teachers/registry.py — V69 M62: the closed set of teachers.

WHY A CLOSED REGISTRY
---------------------
"Which teachers reviewed this?" has to have an answer that a record can be checked
against years later. An open plugin surface makes that answer "whatever was installed
that day", and a review attributed to a provider nobody can identify is not evidence of
anything.

So :data:`TEACHER_PROVIDER_IDS` is frozen, a duplicate registration is refused rather
than resolved by last-wins — last-wins would let a second registration of
``openai_cloud`` quietly become the thing that answers as ``openai_cloud`` — and an
unknown id is a hard error rather than a skipped provider, because a typo must never
silently reduce the set of reviewers a policy believes it required.

NOTHING HAPPENS AT IMPORT
-------------------------
The registry holds FACTORIES. Importing it constructs no provider, opens no network
connection, reads no environment variable and touches no credential. A provider is built
only when :meth:`TeacherRegistry.create` is called with the arguments the caller chose,
and :meth:`TeacherRegistry.describe` reports capability from CLASS attributes so an
operator can list what exists without instantiating anything.

The mock provider is registered but marked test-only, and :meth:`create` refuses it
unless the caller passes ``allow_test_providers=True``: a fabricated review with a real
provider label is the exact thing this whole layer exists to prevent.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ..schemas import require_id
from .anthropic_teacher import OptionalAnthropicTeacherProvider
from .base import (
    TeacherCapability,
    TeacherError,
    TeacherKind,
    TeacherProvider,
)
from .mock_teacher import MockTeacherProvider
from .openai_teacher import OptionalOpenAITeacherProvider
from .verifier_teacher import VerifierTeacherProvider

#: Every provider id that may ever appear in a record. Frozen.
TEACHER_PROVIDER_IDS: tuple[str, ...] = (
    "manual_packet", "verifier_local", "mock_teacher", "openai_cloud",
    "anthropic_cloud",
)


class RegistryError(TeacherError):
    """A registry operation was refused. Fail-closed, never a warning."""


@dataclass(frozen=True)
class ProviderEntry:
    """One registered provider: what it is, and how to build it — not an instance."""

    provider_id: str
    provider_class: type[TeacherProvider] | None
    factory: Callable[..., TeacherProvider] | None = None
    test_only: bool = False
    #: Present for ``manual_packet``, which has no adapter class: the human IS the
    #: transport. Recorded so the registry can describe it without pretending it can
    #: build one.
    offline_only: bool = False
    note: str = ""

    def describe(self) -> dict:
        """Capability from class attributes. Instantiates nothing, reads no secret."""
        cls = self.provider_class
        if cls is None:
            return {"provider_id": self.provider_id, "provider_kind":
                    TeacherKind.MANUAL_PACKET.value, "is_cloud": False,
                    "cost_bearing": False, "test_only": self.test_only,
                    "offline_only": True, "constructible": False, "note": self.note}
        return {"provider_id": self.provider_id,
                "provider_version": cls.provider_version,
                "provider_kind": cls.provider_kind.value,
                "is_cloud": cls.is_cloud,
                "cost_bearing": cls.cost_bearing,
                "deterministic": cls.deterministic,
                "review_modes": [m.value for m in cls.supported_modes],
                "test_only": self.test_only,
                "offline_only": self.offline_only,
                "constructible": True,
                "note": self.note}


class TeacherRegistry:
    """The closed set of providers, and the only way to build one."""

    def __init__(self, entries: Mapping[str, ProviderEntry] | None = None) -> None:
        self._entries: dict[str, ProviderEntry] = {}
        for entry in (entries or {}).values():
            self.register(entry)

    # -- registration ----------------------------------------------------------
    def register(self, entry: ProviderEntry) -> ProviderEntry:
        provider_id = require_id(entry.provider_id, "registry.provider_id")
        if provider_id not in TEACHER_PROVIDER_IDS:
            raise RegistryError(
                f"registry: unknown provider id {provider_id!r}; the frozen set is "
                f"{list(TEACHER_PROVIDER_IDS)}. A provider nobody can name in a policy "
                f"is a reviewer whose absence nobody would notice.")
        if provider_id in self._entries:
            raise RegistryError(
                f"registry: {provider_id!r} is already registered. Resolving a duplicate "
                f"by 'last wins' would let a second registration quietly become the "
                f"thing that answers under an established provider's name.")
        if (entry.provider_class is not None
                and entry.provider_class.provider_id != provider_id):
            raise RegistryError(
                f"registry: {entry.provider_class.__name__} declares provider_id "
                f"{entry.provider_class.provider_id!r}, registered as {provider_id!r}")
        self._entries[provider_id] = entry
        return entry

    # -- queries ---------------------------------------------------------------
    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))

    def entry(self, provider_id: str) -> ProviderEntry:
        key = require_id(provider_id, "registry.provider_id")
        entry = self._entries.get(key)
        if entry is None:
            raise RegistryError(
                f"registry: no provider {key!r}; known providers are {list(self.ids())}. "
                f"An unknown provider fails closed rather than being skipped, because a "
                f"skipped reviewer silently weakens the quorum a policy asked for.")
        return entry

    def describe(self, provider_id: str | None = None) -> list[dict]:
        """List capability without constructing anything or reading a credential."""
        if provider_id is not None:
            return [self.entry(provider_id).describe()]
        return [self._entries[key].describe() for key in self.ids()]

    def is_cloud(self, provider_id: str) -> bool:
        cls = self.entry(provider_id).provider_class
        return bool(cls is not None and cls.is_cloud)

    # -- construction ----------------------------------------------------------
    def create(self, provider_id: str, *, allow_test_providers: bool = False,
               **kwargs: object) -> TeacherProvider:
        """Build one provider. Never builds a mock unless explicitly permitted."""
        entry = self.entry(provider_id)
        if entry.test_only and not allow_test_providers:
            raise RegistryError(
                f"registry: {provider_id!r} is a test double and is refused in normal "
                f"operation; a fabricated review carrying a real provider label is "
                f"exactly what this layer exists to prevent")
        if entry.factory is not None:
            provider = entry.factory(**kwargs)
        elif entry.provider_class is not None:
            provider = entry.provider_class(**kwargs)  # type: ignore[arg-type]
        else:
            raise RegistryError(
                f"registry: {provider_id!r} has no adapter to construct — it is the "
                f"offline manual-packet workflow, where the operator is the transport. "
                f"Export a packet instead.")
        if not isinstance(provider, TeacherProvider):
            raise RegistryError(f"registry: {provider_id!r} produced "
                                f"{type(provider).__name__}, not a TeacherProvider")
        if provider.provider_id != provider_id:
            raise RegistryError(
                f"registry: {provider_id!r} produced a provider identifying as "
                f"{provider.provider_id!r}; refusing a substituted provider")
        return provider

    def capability(self, provider_id: str, **kwargs: object) -> TeacherCapability:
        """Build a provider just far enough to ask whether it is available.

        Separate from :meth:`describe` because availability is a fact about THIS host —
        a wired transport, a reachable credential — and describing the class cannot
        answer it.
        """
        return self.create(provider_id, allow_test_providers=True,
                           **kwargs).capability()


def default_registry() -> TeacherRegistry:
    """The registry the application uses. Built fresh, so no global state is shared.

    A module-level singleton would let one caller's registration decisions leak into
    another's — including a test's registration of a mock into production code that
    imported the same object.
    """
    registry = TeacherRegistry()
    registry.register(ProviderEntry(
        provider_id="manual_packet", provider_class=None, offline_only=True,
        note="export a packet, paste it into ChatGPT or Claude, import the answer; no "
             "credential and no automated egress exist on this path"))
    registry.register(ProviderEntry(
        provider_id="verifier_local", provider_class=VerifierTeacherProvider,
        note="the local VERIFIER-role model; unavailable unless a runtime seam is wired"))
    registry.register(ProviderEntry(
        provider_id="mock_teacher", provider_class=MockTeacherProvider, test_only=True,
        note="deterministic test double; refused unless allow_test_providers=True"))
    registry.register(ProviderEntry(
        provider_id="openai_cloud", provider_class=OptionalOpenAITeacherProvider,
        note="optional, disabled by default, requires --allow-cloud-teachers"))
    registry.register(ProviderEntry(
        provider_id="anthropic_cloud", provider_class=OptionalAnthropicTeacherProvider,
        note="optional, disabled by default, requires --allow-cloud-teachers"))
    return registry


__all__ = ["TEACHER_PROVIDER_IDS", "ProviderEntry", "RegistryError",
           "TeacherRegistry", "default_registry"]
