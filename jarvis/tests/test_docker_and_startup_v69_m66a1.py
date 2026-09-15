"""tests/test_docker_and_startup_v69_m66a1.py — V69 M66A.1 §F4/§F7/§24/§26.

Docker build-context truth and base-profile startup truth.

* Docker: the build context must exclude secrets/state/generated artifacts and
  keep runtime assets. A declaration check runs everywhere (and is the mutation
  detector for ".dockerignore canary allowed"); a real `docker build` canary scan
  runs when a daemon is reachable.
* Startup: `python main.py --smoke-text` must reach the TEXT_READY lifecycle
  milestone and print JARVIS_TEXT_READY_SMOKE_OK — the real entrypoint reaching a
  real milestone, not an import substitute.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_JARVIS = Path(__file__).resolve().parent.parent
_DOCKERIGNORE = _JARVIS / ".dockerignore"

# Canary categories that MUST be excluded from the build context.
_MUST_EXCLUDE = [".env", ".env.*", "logs", "*.log", "*.db", "*.sqlite*", "data",
                 "state", ".git", "__pycache__", "tests", "training",
                 "evaluation", ".venv", "core/telemetry_keys.json"]
# Runtime trees that must NOT be excluded.
_MUST_KEEP = ["aura", "core", "tools", "main.py"]


class TestDockerignoreDeclaration:
    def test_dockerignore_exists(self):
        assert _DOCKERIGNORE.exists(), "no .dockerignore at the build-context root"

    @pytest.mark.parametrize("pat", _MUST_EXCLUDE)
    def test_excludes_canary(self, pat):
        text = _DOCKERIGNORE.read_text()
        lines = {ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")}
        assert pat in lines, f".dockerignore does not exclude '{pat}'"

    @pytest.mark.parametrize("keep", _MUST_KEEP)
    def test_does_not_exclude_runtime(self, keep):
        lines = {ln.strip() for ln in _DOCKERIGNORE.read_text().splitlines()
                 if ln.strip() and not ln.startswith("#")}
        assert keep not in lines, f".dockerignore wrongly excludes runtime '{keep}'"


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True,
                              timeout=15).returncode == 0
    except Exception:
        return False


@pytest.mark.skipif(not _docker_ok(), reason="docker daemon unavailable")
def test_real_docker_context_excludes_canaries(tmp_path):
    """Real build: plant gitignored canaries, build a context-lister, assert the
    secrets never enter the context and the runtime assets do."""
    planted = []
    canaries = {".env": "ENV_CANARY=x\n",
                "logs/debug.log": "log\n",
                "data/state.db": "db\n"}
    try:
        for rel, body in canaries.items():
            p = _JARVIS / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)
            planted.append(p)
        df = tmp_path / "Dockerfile.probe"
        df.write_text(
            "FROM alpine:3.22\nCOPY . /ctx\n"
            "RUN for f in .env logs/debug.log data/state.db; do "
            "[ -e /ctx/$f ] && echo LEAKED:$f || echo ABSENT:$f; done && "
            "test -f /ctx/aura/index.html && echo OK:aura")
        out = subprocess.run(
            ["docker", "build", "--no-cache", "--progress=plain", "-f", str(df),
             "-t", "jarvis-m66a1-pytest-probe", "."],
            cwd=str(_JARVIS), capture_output=True, text=True, timeout=180)
        combined = out.stdout + out.stderr
        subprocess.run(["docker", "rmi", "-f", "jarvis-m66a1-pytest-probe"],
                       capture_output=True)
        # Match the CONCRETE interpolated tokens (a real leak prints the actual
        # filename), not the echoed RUN command which literally contains "LEAKED:$f".
        leaked = [f"LEAKED:{rel}" for rel in canaries
                  if f"LEAKED:{rel}" in combined]
        assert not leaked, f"canaries reached the build context: {leaked}\n{combined}"
        assert "ABSENT:.env" in combined, f"probe did not run as expected:\n{combined}"
        assert "OK:aura" in combined
    finally:
        for p in planted:
            try:
                p.unlink()
            except OSError:
                pass
        for d in ("logs", "data"):
            try:
                (_JARVIS / d).rmdir()
            except OSError:
                pass


class TestBaseStartupSmoke:
    def test_smoke_text_reaches_text_ready(self):
        """The real entrypoint reaches the TEXT_READY milestone and exits 0.
        Bounded (network-free); proves the boot import graph is startable."""
        env = dict(os.environ)
        env["JARVIS_NO_GREETING"] = "1"
        proc = subprocess.run(
            [sys.executable, str(_JARVIS / "main.py"), "--smoke-text", "--no-aura"],
            cwd=str(_JARVIS), capture_output=True, text=True, timeout=180,
            env=env, stdin=subprocess.DEVNULL)
        combined = proc.stdout + proc.stderr
        assert "JARVIS_TEXT_READY_SMOKE_OK" in combined, (
            f"base startup did not reach TEXT_READY (exit {proc.returncode}):\n"
            f"{combined[-1500:]}")
        assert proc.returncode == 0
