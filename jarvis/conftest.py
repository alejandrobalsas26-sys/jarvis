import sys
import os

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
sys.path.insert(0, os.path.join(os.path.abspath(os.path.dirname(__file__)), "brain", "docs", "intrusion-detection-system"))


import pytest


@pytest.fixture(autouse=True)
def _isolated_effect_journal(tmp_path_factory, monkeypatch):
    """V69 M65C — every test gets its own durable effect journal.

    The journal is durable BY DESIGN and retains committed identities forever
    (§29), so without this fixture the suite would share one database: a second
    test invoking the same tool with the same arguments under the same epoch
    would be deduplicated by the first test's committed row, and the whole suite
    would depend on execution order. It would also write to the developer's real
    `jarvis/data/effect_journal.db`.

    §28: tests use temporary directories; the production default stays
    configurable and stable.
    """
    root = tmp_path_factory.mktemp("effect-journal")
    monkeypatch.setenv("JARVIS_EFFECT_JOURNAL_PATH", str(root / "effects.db"))
    yield
