"""tools/threat_feed_sync.py — Live OSINT threat feed aggregator with safe hot YARA injection."""

import asyncio
import ipaddress
import shutil
import tempfile
from pathlib import Path

import aiohttp
import feedparser
import yara

from core.config import settings
from core.events import make_event

_SIG_DIR     = Path(__file__).parent.parent / "core" / "signatures"
_DYNAMIC_YAR = _SIG_DIR / "threatfeed_dynamic.yar"


def _valid_ip(s: str) -> bool:
    try:
        ipaddress.ip_address(s)
        return True
    except ValueError:
        return False


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
                # 1. Abuse.ch Feodo malicious IPs
                async with session.get(
                    "https://feodotracker.abuse.ch/downloads/ipblocklist.json",
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as r:
                    ip_data = await r.json(content_type=None)
                malicious_ips = [
                    e["ip_address"] for e in ip_data
                    if _valid_ip(e.get("ip_address", ""))
                ][:500]

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

                # 4. CISA RSS headlines
                loop = asyncio.get_running_loop()
                feed = await loop.run_in_executor(
                    None,
                    lambda: feedparser.parse(
                        "https://www.cisa.gov/cybersecurity-advisories/all.xml"
                    ),
                )
                for entry in feed.entries[:5]:
                    await broadcast_fn(make_event(
                        "threat_feed_update",
                        source="CISA",
                        alert_title=entry.get("title", "")[:120],
                        severity="ALERT",
                    ))

            except Exception as e:
                await broadcast_fn(make_event("error", error=f"Threat feed sync failed: {e}"))

            await asyncio.sleep(settings.threat_feed_sync_interval)
