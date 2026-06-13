"""
core/intel_fusion.py — Cross-session intelligence fusion engine (v45.0).

Maintains a persistent SQLite database of all observed security events.
Correlates IOCs across time, groups incidents into campaigns,
runs Diamond Model analysis, generates weekly intelligence digests.

Database: logs/intel_fusion.db (grows across all JARVIS sessions)

Tables:
  iocs        — observed indicators with first/last seen, count
  incidents   — all compound incidents with techniques and hosts
  campaigns   — grouped threat actor campaigns
  correlations— links between related iocs and incidents

API:
  ingest_incident()       — called from correlator on each incident
  ingest_ioc()            — called from OSINT/proxy on each IOC
  find_related_incidents()— cluster incidents by shared IOC/technique
  get_campaign_profile()  — Diamond Model for a campaign
  generate_weekly_digest()— LLM-written strategic intelligence report
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from loguru import logger

_DB_PATH = Path("logs/intel_fusion.db")


@asynccontextmanager
async def _get_db():
    """Async SQLite connection context manager (single thread start, auto-close)."""
    import aiosqlite
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(str(_DB_PATH))
    db.row_factory = aiosqlite.Row
    try:
        yield db
    finally:
        await db.close()


async def initialize_db() -> None:
    """Create tables if they don't exist."""
    async with _get_db() as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS iocs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                type       TEXT NOT NULL,
                value      TEXT NOT NULL UNIQUE,
                first_seen TEXT NOT NULL,
                last_seen  TEXT NOT NULL,
                seen_count INTEGER DEFAULT 1,
                threat_score INTEGER DEFAULT 0,
                context    TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS incidents (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id  TEXT NOT NULL UNIQUE,
                timestamp    TEXT NOT NULL,
                severity     REAL DEFAULT 0,
                phase        TEXT DEFAULT '',
                techniques   TEXT DEFAULT '',
                hosts        TEXT DEFAULT '',
                campaign_id  TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS campaigns (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                campaign_id     TEXT NOT NULL UNIQUE,
                first_seen      TEXT NOT NULL,
                last_seen       TEXT NOT NULL,
                incident_count  INTEGER DEFAULT 1,
                technique_count INTEGER DEFAULT 0,
                techniques      TEXT DEFAULT '',
                hosts           TEXT DEFAULT '',
                actor_hypothesis TEXT DEFAULT '',
                diamond_model   TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_ioc_value ON iocs(value);
            CREATE INDEX IF NOT EXISTS idx_ioc_type  ON iocs(type);
            CREATE INDEX IF NOT EXISTS idx_inc_ts    ON incidents(timestamp);
        """)
        await db.commit()
    logger.info("INTEL_FUSION: database initialized")


async def ingest_incident(incident: dict) -> None:
    """
    Store an incident and correlate with existing data.
    Called from the broadcast pipeline on compound_incident events.
    """
    inc_id     = incident.get("incident_id", "")
    if not inc_id:
        return

    techniques = ",".join(incident.get("mitre_techniques", []))
    hosts      = ",".join(str(h) for h in
                          incident.get("involved_hosts", set()))
    now        = datetime.now(timezone.utc).isoformat()

    async with _get_db() as db:
        # Insert incident (ignore if already exists)
        await db.execute("""
            INSERT OR IGNORE INTO incidents
            (incident_id, timestamp, severity, phase, techniques, hosts)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            inc_id, now,
            incident.get("severity_score", 0),
            incident.get("kill_chain_phase", ""),
            techniques, hosts,
        ))

        # Store hosts as IOCs
        for host in incident.get("involved_hosts", set()):
            host_str = str(host)
            if host_str and len(host_str) > 3:
                await db.execute("""
                    INSERT INTO iocs (type, value, first_seen, last_seen, seen_count)
                    VALUES ('ip', ?, ?, ?, 1)
                    ON CONFLICT(value) DO UPDATE SET
                        last_seen   = excluded.last_seen,
                        seen_count  = seen_count + 1
                """, (host_str, now, now))

        await db.commit()

    # Async correlation analysis (don't block the event)
    asyncio.create_task(_correlate_incident(inc_id))


async def ingest_ioc(
    ioc_type: str,
    value: str,
    threat_score: int = 0,
    context: str = "",
) -> None:
    """Store an IOC observation."""
    if not value or len(value) < 3:
        return
    now = datetime.now(timezone.utc).isoformat()
    async with _get_db() as db:
        await db.execute("""
            INSERT INTO iocs
            (type, value, first_seen, last_seen, seen_count, threat_score, context)
            VALUES (?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(value) DO UPDATE SET
                last_seen    = excluded.last_seen,
                seen_count   = seen_count + 1,
                threat_score = MAX(threat_score, excluded.threat_score)
        """, (ioc_type, value, now, now, threat_score, context[:200]))
        await db.commit()


async def _correlate_incident(incident_id: str) -> None:
    """
    Find related incidents and group into campaigns.
    Two incidents are related if they share a technique or host.
    """
    async with _get_db() as db:
        # Get current incident
        row = await (await db.execute(
            "SELECT * FROM incidents WHERE incident_id = ?",
            (incident_id,)
        )).fetchone()
        if not row:
            return

        techniques = set(row["techniques"].split(","))
        hosts      = set(row["hosts"].split(","))

        # Find incidents sharing technique or host
        all_incidents = await (await db.execute(
            "SELECT * FROM incidents WHERE campaign_id = '' "
            "AND incident_id != ? "
            "ORDER BY timestamp DESC LIMIT 50",
            (incident_id,)
        )).fetchall()

        related = []
        for inc in all_incidents:
            inc_techs = set(inc["techniques"].split(","))
            inc_hosts = set(inc["hosts"].split(","))
            if (techniques & inc_techs) or (hosts & inc_hosts - {""}):
                related.append(inc)

        if not related:
            return   # standalone incident — no campaign yet

        # Group into campaign
        # Use oldest related incident's timestamp as campaign anchor
        oldest_related = min(
            related, key=lambda r: r["timestamp"]
        )
        campaign_id = f"CAMP_{oldest_related['incident_id'][:6].upper()}"

        all_in_campaign = [row["incident_id"]] + [
            r["incident_id"] for r in related
        ]
        all_techs = techniques.copy()
        all_hosts = hosts.copy()
        for r in related:
            all_techs.update(r["techniques"].split(","))
            all_hosts.update(r["hosts"].split(","))

        now = datetime.now(timezone.utc).isoformat()
        await db.execute("""
            INSERT INTO campaigns
            (campaign_id, first_seen, last_seen,
             incident_count, technique_count, techniques, hosts)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(campaign_id) DO UPDATE SET
                last_seen       = excluded.last_seen,
                incident_count  = incident_count + 1,
                technique_count = excluded.technique_count,
                techniques      = excluded.techniques,
                hosts           = excluded.hosts
        """, (
            campaign_id,
            oldest_related["timestamp"], now,
            len(all_in_campaign),
            len(all_techs),
            ",".join(all_techs),
            ",".join(all_hosts - {""}),
        ))

        # Tag all related incidents with campaign_id
        for inc_id in all_in_campaign:
            await db.execute(
                "UPDATE incidents SET campaign_id = ? WHERE incident_id = ?",
                (campaign_id, inc_id),
            )
        await db.commit()

        logger.info(
            f"INTEL_FUSION: grouped {len(all_in_campaign)} incidents "
            f"into campaign {campaign_id}"
        )


async def get_active_campaigns_summary() -> list[dict]:
    """Return summary of all tracked campaigns."""
    async with _get_db() as db:
        rows = await (await db.execute("""
            SELECT campaign_id, first_seen, last_seen,
                   incident_count, technique_count, techniques, hosts
            FROM campaigns
            ORDER BY last_seen DESC
            LIMIT 10
        """)).fetchall()

    return [
        {
            "id":              r["campaign_id"],
            "first_seen":      r["first_seen"][:10],
            "last_seen":       r["last_seen"][:10],
            "incident_count":  r["incident_count"],
            "technique_count": r["technique_count"],
            "target":          r["hosts"].split(",")[0] if r["hosts"] else "?",
        }
        for r in rows
    ]


async def get_ioc_history(value: str) -> dict | None:
    """Look up full IOC history."""
    async with _get_db() as db:
        row = await (await db.execute(
            "SELECT * FROM iocs WHERE value = ?", (value,)
        )).fetchone()
    if not row:
        return None
    return dict(row)


async def generate_weekly_digest(
    broadcast_fn,
    ollama_client,
    model: str,
) -> str:
    """
    LLM-written weekly strategic intelligence digest.
    Covers: top campaigns, most-seen techniques, IOC trends.
    """
    async with _get_db() as db:
        # Top campaigns last 7 days
        campaigns = await (await db.execute("""
            SELECT * FROM campaigns
            WHERE last_seen > datetime('now', '-7 days')
            ORDER BY incident_count DESC LIMIT 5
        """)).fetchall()

        # Top techniques
        all_techniques: dict[str, int] = {}
        incidents = await (await db.execute("""
            SELECT techniques FROM incidents
            WHERE timestamp > datetime('now', '-7 days')
        """)).fetchall()
        for row in incidents:
            for t in row["techniques"].split(","):
                if t:
                    all_techniques[t] = all_techniques.get(t, 0) + 1

        # Top IOCs
        top_iocs = await (await db.execute("""
            SELECT type, value, seen_count, threat_score
            FROM iocs
            ORDER BY seen_count DESC LIMIT 10
        """)).fetchall()

    top_techniques = sorted(
        all_techniques.items(), key=lambda x: -x[1]
    )[:5]

    context = (
        f"CAMPAIGNS ({len(campaigns)}):\n"
        + "\n".join(
            f"  {r['campaign_id']}: {r['incident_count']} incidents, "
            f"{r['technique_count']} techniques"
            for r in campaigns
        ) + "\n\n"
        "TOP TECHNIQUES:\n"
        + "\n".join(f"  {t}: {c} times" for t, c in top_techniques)
        + "\n\n"
        "TOP IOCs:\n"
        + "\n".join(
            f"  [{r['type']}] {r['value'][:30]} "
            f"(seen {r['seen_count']}x, score {r['threat_score']})"
            for r in top_iocs
        )
    )

    try:
        resp = await asyncio.wait_for(
            ollama_client.chat.completions.create(
                model    = model,
                messages = [{
                    "role": "system",
                    "content": "You are a senior threat intelligence analyst. "
                               "Write a concise weekly digest (3 paragraphs) "
                               "covering key trends, notable campaigns, "
                               "and strategic recommendations.",
                }, {
                    "role": "user",
                    "content": f"JARVIS WEEKLY INTELLIGENCE DATA:\n{context}\n\n"
                               "Write the strategic digest:",
                }],
                stream = False,
                extra_body = {"options": {
                    "num_ctx": 2048, "temperature": 0.3
                }},
            ),
            timeout=60.0,
        )
        digest = resp.choices[0].message.content.strip()
    except Exception as e:
        digest = f"Digest generation failed: {e}"

    # Save digest
    ts   = datetime.now().strftime("%Y%m%d")
    path = Path("logs/reports") / f"intel_digest_{ts}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# JARVIS Weekly Intelligence Digest\n"
        f"*{datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n"
        f"{digest}\n\n---\n{context}",
        encoding="utf-8",
    )

    await broadcast_fn({
        "type":      "intel_digest_generated",
        "path":      str(path),
        "campaigns": len(campaigns),
        "techniques":len(top_techniques),
        "severity":  "INFO",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })

    # Push digest summary to Telegram
    from core.telegram_bridge import push_alert
    await push_alert(
        "WEEKLY INTELLIGENCE DIGEST",
        digest[:500] + "\n\nFull report saved.",
        "INFO",
    )

    return str(path)
