"""tests/test_plugin_exec_v69_m617.py — V69 M61.7: the plugin loader executes nothing.

Bandit flagged ``core/plugin_loader.py:78`` (B102, ``exec``). Treated as a security
boundary rather than a lint complaint, the boundary turned out not to exist. The old
module docstring claimed "cryptographically-verified" plugins, a "signed manifest" and
a "RESTRICTED exec environment" with ``open``, ``os``, ``subprocess``, ``socket``,
``ctypes`` and ``importlib`` BLOCKED. Four findings, in order of severity:

  1. **The sandbox was not a sandbox.** ``exec(code, {"__builtins__": _SAFE_BUILTINS,
     "re": re, "json": json, ...})`` restricts a *namespace*, not privileges. Any
     function reachable from those injected modules carries
     ``__globals__["__builtins__"]`` — the real builtins, ``__import__`` included.
     ``().__class__.__base__.__subclasses__()`` is an independent second route. Both
     were verified working against the shipped configuration: three lines inside the
     "sandbox" imported ``os``, read arbitrary files and spawned a subprocess.
  2. **The manifest was never signed.** The expected SHA-256 lived in
     ``plugins/manifest.json``, in the same directory as the plugin, so whoever could
     write the plugin could write its hash.
  3. **The directory was redirectable** via ``JARVIS_PLUGIN_DIR``.
  4. **No operator was in the loop.** ``start()`` loaded at boot and a watchdog
     hot-reloaded on any file change.

That is arbitrary code execution in-process, with JARVIS's credentials and network
reach, gated only by write access to a directory. M61 is a stabilization release, so
the fix is the smallest safe one from the escalation ladder: refuse to execute.

These tests assert the refusal **structurally** (no ``exec``/``eval``/``compile`` node
survives anywhere in the module, and no environment variable re-enables it) and
**behaviourally** (a corpus of real escape payloads is inert). A structural test alone
could be satisfied by a rename; a behavioural test alone could be satisfied by a
denylist. Both together cannot.
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import json
from pathlib import Path

import pytest

import core.plugin_loader as pl

_APP_ROOT = Path(__file__).resolve().parent.parent
_MODULE = _APP_ROOT / "core" / "plugin_loader.py"


# ── the escape corpus: payloads that DID work against the old sandbox ───────
#: Each entry is a plugin body whose ``analyze`` escaped the "restricted" exec
#: environment. They are kept as source strings precisely because nothing compiles
#: them any more — that is the property under test.
_ESCAPE_PAYLOADS: dict[str, str] = {
    "module_globals_to_real_builtins": (
        "def analyze(e):\n"
        "    b = json.dumps.__globals__['__builtins__']\n"
        "    imp = b['__import__'] if isinstance(b, dict) else b.__import__\n"
        "    return {'os_name': imp('os').name}\n"
    ),
    "spawn_subprocess": (
        "def analyze(e):\n"
        "    b = re.match.__globals__['__builtins__']\n"
        "    imp = b['__import__'] if isinstance(b, dict) else b.__import__\n"
        "    sp = imp('subprocess')\n"
        "    return {'rc': sp.run(['whoami'], capture_output=True).returncode}\n"
    ),
    "read_arbitrary_file": (
        "def analyze(e):\n"
        "    b = hashlib.sha256.__globals__['__builtins__']\n"
        "    op = b['open'] if isinstance(b, dict) else b.open\n"
        "    return {'stolen': op('plugins/manifest.json').read()}\n"
    ),
    "subclass_walk": (
        "def analyze(e):\n"
        "    return {'n': len(().__class__.__base__.__subclasses__())}\n"
    ),
    "exfiltrate_process_environment": (
        "def analyze(e):\n"
        "    b = json.dumps.__globals__['__builtins__']\n"
        "    imp = b['__import__'] if isinstance(b, dict) else b.__import__\n"
        "    return {'env': dict(imp('os').environ)}\n"
    ),
    "mutate_authority_at_import_time": (
        "b = json.dumps.__globals__['__builtins__']\n"
        "imp = b['__import__'] if isinstance(b, dict) else b.__import__\n"
        "imp('core.authority').set_mode('trusted_lab')\n"
        "def analyze(e):\n"
        "    return None\n"
    ),
    "syntactically_invalid": "def analyze(e:\n    ???\n",
    "well_formed_and_harmless": "def analyze(e):\n    return {'ok': 1}\n",
}


@pytest.mark.parametrize("label", sorted(_ESCAPE_PAYLOADS))
def test_no_plugin_payload_is_ever_compiled_or_run(label: str, capsys):
    """Every payload — hostile, malformed and benign alike — returns ``None``.

    The malformed one matters: if the source were still compiled, a ``SyntaxError``
    would be raised or logged, which would prove the text had been parsed. And the
    benign one matters: refusal must be unconditional, not a validation failure that
    a well-behaved plugin could pass.
    """
    result = pl._compile_one(label, _ESCAPE_PAYLOADS[label])
    assert result is None
    # Nothing from the payload reached stdout (the old sandbox exposed ``print``).
    assert "os_name" not in capsys.readouterr().out


def test_the_authority_mutating_payload_did_not_change_authority():
    """The most damaging payload class: a plugin escalating JARVIS's own authority."""
    from core import authority

    before = authority.get_mode() if hasattr(authority, "get_mode") else None
    assert pl._compile_one("evil", _ESCAPE_PAYLOADS["mutate_authority_at_import_time"]) is None
    after = authority.get_mode() if hasattr(authority, "get_mode") else None
    assert before == after


def test_compile_one_does_not_even_read_its_source_argument():
    """``code_text`` is inert: an object that explodes when used still returns None."""

    class _Explodes:
        def __getattr__(self, name):  # pragma: no cover — must never be reached
            raise AssertionError(f"plugin source was inspected via .{name}")

        def __iter__(self):  # pragma: no cover
            raise AssertionError("plugin source was iterated")

    assert pl._compile_one("inert", _Explodes()) is None


# ── structural guarantees over the module ───────────────────────────────────
def test_module_contains_no_dynamic_execution_node():
    """AST-level: no exec/eval/compile/__import__ call survives in the loader."""
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    banned = {"exec", "eval", "compile", "__import__"}
    offenders = [
        f"{node.func.id}@{node.lineno}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in banned
    ]
    assert offenders == [], f"core/plugin_loader.py still executes source: {offenders}"


def _environment_variable_names_read_by_the_loader() -> list[str]:
    """Every env var name the loader reads: ``os.getenv(X)`` / ``os.environ.get(X)``.

    Scoped to genuine environment access — a ``dict.get("enabled")`` on a parsed
    manifest is a data field, not a configuration switch, and must not be confused
    for one.
    """
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and node.args):
            continue
        func = node.func
        is_getenv = isinstance(func, ast.Attribute) and func.attr == "getenv"
        is_environ_get = (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "environ"
        )
        is_environ_sub = isinstance(func, ast.Subscript)
        if (is_getenv or is_environ_get or is_environ_sub) and isinstance(
            node.args[0], ast.Constant
        ):
            names.append(str(node.args[0].value))
    return names


def test_no_environment_variable_can_re_enable_execution():
    """An opt-in flag would be the same vulnerability behind a different default."""
    names = _environment_variable_names_read_by_the_loader()
    # The only env var this module may read is the plugin directory override, which
    # relocates the INVENTORY and can no longer cause anything to run.
    assert names == ["JARVIS_PLUGIN_DIR"], names
    assert pl.DYNAMIC_EXEC_SUPPORTED is False


def test_dynamic_exec_flag_is_a_literal_false_not_a_computation():
    """So it cannot become True through configuration, monkeypatching or import order."""
    tree = ast.parse(_MODULE.read_text(encoding="utf-8"))
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "DYNAMIC_EXEC_SUPPORTED"
            for t in node.targets
        )
    ]
    assert len(assignments) == 1
    assert isinstance(assignments[0].value, ast.Constant)
    assert assignments[0].value.value is False


def test_the_safe_builtins_illusion_is_gone():
    """``_SAFE_BUILTINS`` promised a boundary it could not provide; it must not return."""
    assert not hasattr(pl, "_SAFE_BUILTINS")
    source = _MODULE.read_text(encoding="utf-8")
    assert "_SAFE_BUILTINS" not in source.split('"""')[-1]


def test_no_module_object_is_injectable_into_a_plugin_namespace():
    """The escape vehicles (``re``, ``time`` injected into exec globals) are gone."""
    assert not hasattr(pl, "re")
    assert not hasattr(pl, "time")


def test_the_docstring_no_longer_claims_a_sandbox_or_a_signature():
    """The previous docstring's claims were false; honesty is part of the fix."""
    docstring = (pl.__doc__ or "").lower()
    assert "execution is disabled" in docstring
    assert "not a privilege boundary" in docstring or "was not a sandbox" in docstring
    # It must not assert protections that never existed.
    assert "cryptographically-verified python plugins" not in docstring


# ── the inventory stays honest ──────────────────────────────────────────────
@pytest.fixture
def manifest_with_one_plugin(tmp_path, monkeypatch):
    """A manifest + plugin file in an isolated directory."""
    plugin_dir = tmp_path / "plugins"
    plugin_dir.mkdir()
    plugin = plugin_dir / "demo.py"
    plugin.write_text("def analyze(e):\n    return {'ok': 1}\n", encoding="utf-8")
    manifest = plugin_dir / "manifest.json"
    manifest.write_text(
        json.dumps([{
            "name": "demo", "file": "demo.py",
            "sha256": hashlib.sha256(plugin.read_bytes()).hexdigest(),
            "version": "9.9", "enabled": True,
        }]),
        encoding="utf-8",
    )
    monkeypatch.setattr(pl, "_PLUGIN_DIR", plugin_dir)
    monkeypatch.setattr(pl, "_MANIFEST", manifest)
    saved = dict(pl.LOADED_PLUGINS)
    pl.LOADED_PLUGINS.clear()
    yield plugin_dir
    pl.LOADED_PLUGINS.clear()
    pl.LOADED_PLUGINS.update(saved)
    pl.REFUSED_PLUGINS.clear()


def test_a_hash_matching_plugin_is_reported_but_not_loaded(manifest_with_one_plugin):
    pl.load_all()
    assert pl.LOADED_PLUGINS == {}
    assert "demo" in pl.REFUSED_PLUGINS
    record = pl.REFUSED_PLUGINS["demo"]
    assert record["integrity"] == "match"
    assert record["version"] == "9.9"
    assert "not a privilege boundary" in record["reason"]


def test_a_tampered_plugin_is_reported_as_a_mismatch(manifest_with_one_plugin):
    (manifest_with_one_plugin / "demo.py").write_text("def analyze(e):\n    pass\n", encoding="utf-8")
    pl.load_all()
    assert pl.REFUSED_PLUGINS["demo"]["integrity"] == "mismatch"
    assert pl.LOADED_PLUGINS == {}


def test_status_reports_refusals_so_a_dashboard_cannot_imply_an_empty_directory(
    manifest_with_one_plugin,
):
    """"0 plugins loaded" would read as "no plugins present". It is not the same thing."""
    pl.load_all()
    payload = pl.status()
    assert payload["dynamic_exec_supported"] is False
    assert payload["loaded"] == {}
    assert "demo" in payload["refused"]


def test_load_all_is_idempotent_and_does_not_accumulate(manifest_with_one_plugin):
    pl.load_all()
    pl.load_all()
    assert len(pl.REFUSED_PLUGINS) == 1


def test_hot_reload_cannot_execute_a_dropped_file(manifest_with_one_plugin):
    """A file appearing in the watched directory must not gain execution."""
    hostile = manifest_with_one_plugin / "dropped.py"
    hostile.write_text(_ESCAPE_PAYLOADS["spawn_subprocess"], encoding="utf-8")
    manifest = manifest_with_one_plugin / "manifest.json"
    manifest.write_text(
        json.dumps([{
            "name": "dropped", "file": "dropped.py",
            "sha256": hashlib.sha256(hostile.read_bytes()).hexdigest(),
            "version": "1.0", "enabled": True,
        }]),
        encoding="utf-8",
    )
    pl._PluginWatcher()._reload()          # exactly what the watchdog calls
    assert pl.LOADED_PLUGINS == {}
    assert pl.REFUSED_PLUGINS["dropped"]["integrity"] == "match"


def test_route_event_is_a_no_op_with_nothing_loaded():
    """The correlator wiring must stay safe, not merely unused."""
    import asyncio

    class _Correlator:
        def __init__(self):
            self.ingested = []

        async def ingest_event(self, event):  # pragma: no cover — must not be called
            self.ingested.append(event)

    correlator = _Correlator()
    pl._correlator = correlator
    pl.LOADED_PLUGINS.clear()
    asyncio.run(pl.route_event({"severity": 10.0, "attck": ["T1055", "T1041"]}))
    assert correlator.ingested == []


def test_the_module_still_imports_cleanly_after_a_reload():
    """Nothing here depends on import-time side effects of a plugin directory."""
    reloaded = importlib.reload(pl)
    assert reloaded.DYNAMIC_EXEC_SUPPORTED is False
