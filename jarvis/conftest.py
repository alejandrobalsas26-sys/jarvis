import sys
import os

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(os.path.abspath(os.path.dirname(__file__)), "brain", "docs", "intrusion-detection-system"))


import hashlib

import pytest


@pytest.fixture(scope="session")
def _effect_journal_root(tmp_path_factory):
    """One temporary directory for the whole session. See below."""
    return tmp_path_factory.mktemp("effect-journals")


@pytest.fixture(autouse=True)
def _isolated_effect_journal(request, _effect_journal_root, monkeypatch):
    """V69 M65C — every test gets its own durable effect journal.

    The journal is durable BY DESIGN and retains committed identities forever
    (§29), so without this fixture the suite would share one database: a second
    test invoking the same tool with the same arguments under the same epoch
    would be deduplicated by the first test's committed row, and the whole suite
    would depend on execution order. It would also write to the developer's real
    `jarvis/data/effect_journal.db`.

    The path is DERIVED from the node id and the directory is not created here.
    Calling `tmp_path_factory.mktemp` per test instead cost the scientific suite
    minutes — measured: it stalled around the halfway mark, because the S4H
    mutation campaign spawns nested pytest runs and every test in every one of
    them paid for a directory it would never use. Only a test that actually
    opens a journal creates anything; `DurableEffectJournal` makes its parents.

    §28: tests use temporary directories; the production default stays
    configurable and stable.
    """
    name = hashlib.sha256(request.node.nodeid.encode()).hexdigest()[:24]
    monkeypatch.setenv("JARVIS_EFFECT_JOURNAL_PATH",
                       str(_effect_journal_root / name / "effects.db"))
    yield
