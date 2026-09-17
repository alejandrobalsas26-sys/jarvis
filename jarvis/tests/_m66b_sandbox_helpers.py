"""tests/_m66b_sandbox_helpers.py — shared harness for the M66B escape matrix.

All fixtures are LOCAL and HARMLESS (§16/§34): synthetic host files, a host
loopback listener on a random high port, an unroutable metadata-shaped address.
No public host is ever probed.
"""
from __future__ import annotations

import os
import socket
import threading

import pytest

from core.containment import (
    BubblewrapBackend,
    ContainmentRequirement,
    ExecutionRequest,
)

SANDBOX = BubblewrapBackend()
SANDBOX_AVAILABLE = SANDBOX.available()

requires_sandbox = pytest.mark.skipif(
    not SANDBOX_AVAILABLE,
    reason="bubblewrap + unprivileged user namespaces not available on this host")


def run_sandboxed(code: str, timeout: int = 15):
    """Run *code* through the real bubblewrap backend; return the ExecutionOutcome."""
    return SANDBOX.execute(ExecutionRequest(code, timeout,
                                            ContainmentRequirement.SANDBOX_REQUIRED))


class HostLoopbackListener:
    """A real host listener on 127.0.0.1:<random>. Its whole purpose is to be
    UNREACHABLE from inside the sandbox — but first it must be provably LIVE, so a
    green result cannot come from a dead fixture (§17 non-vacuity)."""

    def __init__(self, family=socket.AF_INET, host="127.0.0.1"):
        self.family = family
        self.host = host
        self._srv = socket.socket(family, socket.SOCK_STREAM)
        self._srv.bind((host, 0))
        self._srv.listen(8)
        self.port = self._srv.getsockname()[1]
        self.hits = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._accept, daemon=True)
        self._t.start()

    def _accept(self):
        self._srv.settimeout(0.5)
        while not self._stop.is_set():
            try:
                c, _ = self._srv.accept()
                self.hits += 1
                c.close()
            except socket.timeout:
                continue
            except OSError:
                break

    def prove_live(self) -> None:
        """Connect from the HOST to prove the listener works. Raises if it does
        not — a vacuous fixture must fail loudly, not pass silently."""
        s = socket.socket(self.family, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect((self.host, self.port))
        s.close()
        # give the accept loop a moment
        import time
        for _ in range(20):
            if self.hits >= 1:
                break
            time.sleep(0.05)
        if self.hits < 1:
            raise AssertionError("host loopback fixture is not live (vacuous)")

    def close(self):
        self._stop.set()
        try:
            self._srv.close()
        except OSError:
            pass
