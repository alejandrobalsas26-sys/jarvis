"""
core/target_aliases.py — Target alias registry (v44.0).

Name your lab targets once, use names everywhere.
Aliases resolve in: ARES campaigns, BAS simulations, sensor deployment,
OSINT enrichment, and voice commands.

Storage: logs/target_aliases.json (survives restarts)
Voice: "JARVIS remember 192.168.1.100 as victim"
Voice: "JARVIS show targets"
"""

import json
from pathlib import Path
from loguru import logger

from core.managed_paths import log_artifact_path

_aliases: dict[str, str] = {}   # name → ip/hostname


def _aliases_path(*, create: bool = True) -> Path:
    """The managed operator alias registry (V69 M61 RC1).

    Durable operator state: a CWD-relative location made the aliases silently
    disappear whenever JARVIS was launched from a different directory. This
    module loads at import, so the read passes ``create=False`` — importing must
    not write to the filesystem.
    """
    return log_artifact_path("target_aliases.json", create=create)


def _load() -> None:
    global _aliases
    path = _aliases_path(create=False)
    if path.exists():
        try:
            _aliases = json.loads(
                path.read_text(encoding="utf-8")
            )
            logger.info(
                f"TARGET_ALIASES: loaded {len(_aliases)} aliases"
            )
        except Exception:
            pass


def _save() -> None:
    _aliases_path().write_text(
        json.dumps(_aliases, indent=2), encoding="utf-8"
    )


_load()


def add_alias(name: str, target: str) -> None:
    """Add or update an alias."""
    name = name.lower().strip()
    _aliases[name] = target.strip()
    _save()
    logger.info(f"TARGET_ALIASES: {name} → {target}")


def remove_alias(name: str) -> bool:
    """Remove an alias. Returns True if it existed."""
    name = name.lower().strip()
    if name in _aliases:
        del _aliases[name]
        _save()
        return True
    return False


def resolve(name_or_ip: str) -> str:
    """
    Resolve an alias to its target.
    Returns original string if not an alias.
    """
    return _aliases.get(name_or_ip.lower().strip(), name_or_ip)


def list_aliases() -> dict[str, str]:
    return dict(_aliases)


def get_alias_for(target: str) -> str | None:
    """Reverse lookup: find alias name for a target IP/hostname."""
    for name, val in _aliases.items():
        if val == target:
            return name
    return None


async def broadcast_aliases(broadcast_fn) -> None:
    """Send alias list to AURA HUD."""
    await broadcast_fn({
        "type":    "target_aliases_updated",
        "aliases": _aliases,
        "count":   len(_aliases),
    })
