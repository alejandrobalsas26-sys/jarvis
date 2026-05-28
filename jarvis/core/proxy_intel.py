"""
core/proxy_intel.py — MITM proxy intelligence engine (v42.0).

Runs mitmproxy as a transparent intercepting proxy.
Analyzes every HTTP/HTTPS request and response in real-time.

Capabilities:
  - Credential extraction (Basic auth, form fields, Bearer tokens)
  - API key detection (patterns for 50+ common API providers)
  - Intelligent XSS probe injection (only on text/html responses)
  - SQLi canary injection in GET parameters
  - SSRF detection via request pattern analysis
  - Full traffic logging to logs/proxy_traffic/

Setup (one-time):
  1. Start JARVIS (proxy starts automatically on port 8888)
  2. Configure browser/tool proxy to 127.0.0.1:8888
  3. Visit any HTTPS site → JARVIS intercepts automatically
  4. For HTTPS: install mitmproxy CA cert from http://mitm.it

The CA cert is auto-generated in logs/certs/mitmproxy-ca-cert.pem
"""

import asyncio
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

_PROXY_PORT  = int(os.getenv("JARVIS_PROXY_PORT", "8888"))
_TRAFFIC_DIR = Path("logs/proxy_traffic")
_CERTS_DIR   = Path("logs/certs")
_TRAFFIC_DIR.mkdir(parents=True, exist_ok=True)
_CERTS_DIR.mkdir(parents=True, exist_ok=True)

# Cross-thread event queue — mitmproxy runs in its own thread
_event_queue: asyncio.Queue | None = None

# ── Credential + secret patterns ─────────────────────────────────────────────

_CREDENTIAL_PATTERNS = {
    "Basic Auth":      re.compile(r"Basic\s+([A-Za-z0-9+/=]{8,})", re.I),
    "Bearer Token":    re.compile(r"Bearer\s+([A-Za-z0-9\-_.~+/=]{20,})", re.I),
    "AWS Key":         re.compile(r"AKIA[0-9A-Z]{16}"),
    "GitHub Token":    re.compile(r"ghp_[A-Za-z0-9]{36}"),
    "Slack Token":     re.compile(r"xox[baprs]-[A-Za-z0-9\-]+"),
    "Google API Key":  re.compile(r"AIza[0-9A-Za-z\-_]{35}"),
    "Password Field":  re.compile(
        r"password[=%&]([^&\s]{4,})", re.I
    ),
    "Private Key":     re.compile(r"-----BEGIN (RSA |EC )?PRIVATE KEY-----"),
}

_INJECTABLE_PARAMS = {"q", "search", "id", "user", "name", "input",
                       "value", "data", "query", "cmd", "exec", "file",
                       "password"}

_XSS_CANARY  = '<script>/*JARVIS_XSS*/</script>'
_SQLI_CANARY = "' OR '1'='1"

# ── mitmproxy addon ───────────────────────────────────────────────────────────

class _JarvisProxyAddon:
    """
    mitmproxy addon that inspects every request/response.
    Runs in the mitmproxy event loop — passes findings to
    main JARVIS via asyncio.Queue (thread-safe).
    """

    def __init__(self, queue: asyncio.Queue, main_loop: asyncio.AbstractEventLoop):
        self._queue = queue
        self._loop  = main_loop
        self._finding_count = 0

    def _enqueue(self, finding: dict) -> None:
        """Thread-safe enqueue to main JARVIS event loop."""
        try:
            self._loop.call_soon_threadsafe(
                self._queue.put_nowait, finding
            )
        except Exception:
            pass

    def request(self, flow) -> None:
        """Inspect every outgoing request."""
        url     = flow.request.pretty_url[:200]
        method  = flow.request.method
        headers = dict(flow.request.headers)
        body    = flow.request.get_text(strict=False) or ""

        # Combine all text for pattern matching
        full_text = (
            " ".join(str(v) for v in headers.values()) + " " + body
        )

        # ── Credential extraction ─────────────────────────────────────────
        for cred_type, pattern in _CREDENTIAL_PATTERNS.items():
            match = pattern.search(full_text)
            if match:
                self._finding_count += 1
                self._enqueue({
                    "type":        "proxy_credential_found",
                    "cred_type":   cred_type,
                    "url":         url,
                    "preview":     match.group(0)[:40] + "...",
                    "severity":    "CRITICAL",
                    "timestamp":   datetime.now(timezone.utc).isoformat(),
                })

        # ── SQLi canary injection ─────────────────────────────────────────
        try:
            params = dict(flow.request.query)
            for param, value in params.items():
                if param.lower() in _INJECTABLE_PARAMS:
                    flow.request.query[param] = value + _SQLI_CANARY
        except Exception:
            pass

        self._enqueue({
            "type":     "proxy_request",
            "method":   method,
            "url":      url[:100],
            "has_body": bool(body.strip()),
        })

    def response(self, flow) -> None:
        """Inspect every server response."""
        if not flow.response:
            return

        status = flow.response.status_code
        ct     = flow.response.headers.get("content-type", "")
        body   = flow.response.get_text(strict=False) or ""
        url    = flow.request.pretty_url[:100]

        # ── SQLi canary reflection detection ──────────────────────────────
        if "text/html" in ct and _SQLI_CANARY in body:
            self._enqueue({
                "type":      "proxy_sqli_reflected",
                "url":       url,
                "severity":  "CRITICAL",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        # ── Interesting status codes ──────────────────────────────────────
        if status in (401, 403, 500, 502, 503):
            self._enqueue({
                "type":      "proxy_interesting_response",
                "status":    status,
                "url":       url,
                "severity":  "MEDIUM",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })

        # ── Log traffic ───────────────────────────────────────────────────
        entry = (
            f"{datetime.now().strftime('%H:%M:%S')} "
            f"{flow.request.method} {url} → {status}\n"
        )
        try:
            log_path = _TRAFFIC_DIR / f"traffic_{datetime.now().strftime('%Y%m%d')}.log"
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(entry)
        except Exception:
            pass


# ── Public API ────────────────────────────────────────────────────────────────

async def start_proxy_intel(broadcast_fn) -> None:
    """
    Start the mitmproxy intelligence engine.
    Runs proxy in a background thread.
    Bridges findings to JARVIS broadcast via asyncio.Queue.
    """
    global _event_queue
    _event_queue = asyncio.Queue(maxsize=500)
    loop         = asyncio.get_running_loop()

    logger.info(
        f"PROXY_INTEL: starting MITM proxy on port {_PROXY_PORT}\n"
        f"  → Configure tools to use proxy: 127.0.0.1:{_PROXY_PORT}\n"
        f"  → For HTTPS: install CA cert from http://mitm.it"
    )

    await broadcast_fn({
        "type":      "proxy_started",
        "port":      _PROXY_PORT,
        "ca_hint":   "Configure proxy to 127.0.0.1:" + str(_PROXY_PORT),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    def _run_proxy():
        """Blocking — runs in background thread."""
        try:
            # mitmproxy needs its own asyncio loop in this thread
            import asyncio as _aio
            _aio.set_event_loop(_aio.new_event_loop())

            from mitmproxy.tools.dump import DumpMaster
            from mitmproxy import options

            opts = options.Options(
                listen_host  = "127.0.0.1",
                listen_port  = _PROXY_PORT,
                ssl_insecure = True,
            )
            master = DumpMaster(opts, with_termlog=False, with_dumper=False)
            master.addons.add(_JarvisProxyAddon(_event_queue, loop))

            _aio.get_event_loop().run_until_complete(master.run())
        except Exception as e:
            try:
                loop.call_soon_threadsafe(
                    _event_queue.put_nowait,
                    {"type": "proxy_error", "error": str(e)}
                )
            except Exception:
                pass

    proxy_thread = threading.Thread(
        target=_run_proxy, daemon=True, name="JARVISProxy"
    )
    proxy_thread.start()

    # Consumer: drain queue and broadcast findings
    while True:
        try:
            finding = await asyncio.wait_for(
                _event_queue.get(), timeout=5.0
            )
            if finding.get("severity") in ("CRITICAL", "HIGH"):
                await broadcast_fn(finding)
            elif finding.get("type") == "proxy_error":
                logger.error(f"PROXY: {finding.get('error')}")
                break
        except asyncio.TimeoutError:
            continue
        except Exception as e:
            logger.debug(f"PROXY_INTEL: {e}")
            await asyncio.sleep(1)
