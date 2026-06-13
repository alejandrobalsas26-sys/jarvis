"""tools/threat_feed_sync.py — Live OSINT threat feed aggregator with safe hot YARA injection."""

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import aiohttp
import feedparser
import yara

from core.config import settings
from core.events import make_event
from core.feed_sanitizer import (
    sanitize_ioc,
    sanitize_alert_title,
    check_content_hash,
    MAX_IOCS_PER_CYCLE,
)

_SIG_DIR     = Path(__file__).parent.parent / "core" / "signatures"
_DYNAMIC_YAR = _SIG_DIR / "threatfeed_dynamic.yar"


def _build_yara_rule(ips: list[str]) -> str:
    conditions = " or ".join(f"$ip{i}" for i in range(len(ips)))
    strings    = "\n    ".join(f'$ip{i} = "{ip}"' for i, ip in enumerate(ips))
    return (
        "rule ThreatFeed_Dynamic_IPs {\n"
        "  meta:\n    source = \"abuse.ch\"\n"
        f"  strings:\n    {strings}\n"
        f"  condition:\n    {conditions or 'false'}\n"
        "}\n"
    )


async def start_threat_feed_sync(broadcast_fn) -> None:
    _SIG_DIR.mkdir(parents=True, exist_ok=True)
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # 1. Abuse.ch Feodo malicious IPs — read raw bytes first for hash check
                async with session.get(
                    "https://feodotracker.abuse.ch/downloads/ipblocklist.json",
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as r:
                    raw_bytes = await r.read()

                check_content_hash("feodo", raw_bytes)
                ip_data = json.loads(raw_bytes.decode("utf-8", errors="replace"))

                # Sanitize IPs — reject injections, validate format, cap count
                malicious_ips: list[str] = []
                rejected = 0
                for entry in ip_data:
                    raw_ip = entry.get("ip_address", "")
                    sanitized = sanitize_ioc("ip", raw_ip, source="feodo")
                    if sanitized:
                        malicious_ips.append(sanitized)
                    else:
                        rejected += 1
                    if len(malicious_ips) >= MAX_IOCS_PER_CYCLE:
                        break

                if rejected:
                    from loguru import logger
                    logger.warning(f"THREAT_FEED: {rejected} IPs rejected by sanitizer (feodo)")

                # 2. Test-compile candidate rule in isolation
                rule_src = _build_yara_rule(malicious_ips)
                tmp = Path(tempfile.gettempdir()) / "candidate_threatfeed.yar"
                tmp.write_text(rule_src, encoding="utf-8")
                yara.compile(filepath=str(tmp))

                # 3. Atomic swap + cache clear
                shutil.move(str(tmp), str(_DYNAMIC_YAR))
                try:
                    from core.yara_analyzer import get_compiled_rules
                    get_compiled_rules.cache_clear()
                except Exception:
                    pass

                await broadcast_fn(make_event(
                    "threat_feed_update",
                    source="Abuse.ch Feodo",
                    ioc_count=len(malicious_ips),
                    severity="INFO",
                ))

                # 4. CISA RSS headlines — sanitize titles before broadcast
                loop = asyncio.get_running_loop()
                feed = await loop.run_in_executor(
                    None,
                    lambda: feedparser.parse(
                        "https://www.cisa.gov/cybersecurity-advisories/all.xml"
                    ),
                )
                for entry in feed.entries[:5]:
                    raw_title = entry.get("title", "")
                    title = sanitize_alert_title(raw_title, source="CISA")
                    if title:
                        await broadcast_fn(make_event(
                            "threat_feed_update",
                            source="CISA",
                            alert_title=title,
                            severity="ALERT",
                        ))

            except Exception as e:
                await broadcast_fn(make_event("error", error=f"Threat feed sync failed: {e}"))

            await asyncio.sleep(settings.threat_feed_sync_interval)
