"""
tools/diagram_generator.py — Visual diagram generation engine (v38.0).

Generates PNG diagrams from JARVIS operational data:
  - Network topology (from nmap scan results + BloodHound)
  - Attack chain timeline (from compound incidents)
  - MITRE ATT&CK coverage heatmap
  - Connection graph (from Zeek DPI data)
  - QR codes (for phishing simulation payloads)

All outputs saved to logs/visuals/diagrams/
Uses matplotlib + networkx (already installed).
"""

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from loguru import logger

_DIAGRAMS_DIR = Path("logs/visuals/diagrams")
_DIAGRAMS_DIR.mkdir(parents=True, exist_ok=True)


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


async def generate_network_topology(
    hosts: list[dict],
    broadcast_fn,
) -> Path | None:
    """
    Generate network topology diagram from host list.
    hosts: [{"ip": str, "hostname": str, "ports": [int], "role": str}]
    """
    loop = asyncio.get_running_loop()
    path = await loop.run_in_executor(
        None, _draw_topology, hosts
    )
    if path:
        await broadcast_fn({
            "type":      "diagram_generated",
            "diagram":   "network_topology",
            "path":      str(path),
            "hosts":     len(hosts),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    return path


def _draw_topology(hosts: list[dict]) -> Path | None:
    """Blocking — runs in executor."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import networkx as nx

        G   = nx.DiGraph()
        pos = {}

        # Add central JARVIS node
        G.add_node("JARVIS", role="controller")
        pos["JARVIS"] = (0, 0)

        # Role colors
        role_colors = {
            "attacker":    "#ff4400",
            "victim":      "#ff8800",
            "server":      "#4488ff",
            "controller":  "#00ff41",
            "unknown":     "#666666",
        }

        node_colors = ["#00ff41"]   # JARVIS center
        node_labels = {"JARVIS": "JARVIS\nHOST"}

        for i, host in enumerate(hosts[:20]):
            ip   = host.get("ip", f"host_{i}")
            role = host.get("role", "unknown").lower()
            name = host.get("hostname", ip)
            ports = host.get("ports", [])

            G.add_node(ip, role=role)
            G.add_edge("JARVIS", ip)

            angle = (i / max(len(hosts), 1)) * 6.28
            r     = 2.5
            import math
            pos[ip] = (r * math.cos(angle), r * math.sin(angle))
            node_colors.append(role_colors.get(role, "#666666"))
            port_str = ",".join(str(p) for p in ports[:5])
            node_labels[ip] = f"{name[:12]}\n{ip}\n[{port_str}]" if port_str else f"{name[:12]}\n{ip}"

        fig, ax = plt.subplots(figsize=(14, 10), facecolor="#07090f")
        ax.set_facecolor("#07090f")

        nx.draw_networkx_nodes(
            G, pos, node_color=node_colors,
            node_size=1200, ax=ax, alpha=0.9,
        )
        nx.draw_networkx_edges(
            G, pos, edge_color="#00ff4155",
            arrows=True, ax=ax, width=1.5,
        )
        nx.draw_networkx_labels(
            G, pos, labels=node_labels,
            font_size=7, font_color="#ffffff", ax=ax,
        )
        ax.set_title(
            "JARVIS Network Topology",
            color="#00ff41", fontsize=14, pad=15,
        )
        ax.axis("off")
        plt.tight_layout()

        path = _DIAGRAMS_DIR / f"network_topology_{_ts()}.png"
        plt.savefig(str(path), dpi=120, bbox_inches="tight",
                    facecolor="#07090f")
        plt.close()
        logger.info(f"DIAGRAM: network topology → {path.name}")
        return path

    except Exception as e:
        logger.debug(f"DIAGRAM: topology error: {e}")
        return None


async def generate_attack_timeline(
    incident: dict,
    broadcast_fn,
) -> Path | None:
    """Generate a visual attack chain timeline from a compound incident."""
    loop = asyncio.get_running_loop()
    path = await loop.run_in_executor(
        None, _draw_timeline, incident
    )
    if path:
        await broadcast_fn({
            "type":        "diagram_generated",
            "diagram":     "attack_timeline",
            "path":        str(path),
            "incident_id": incident.get("incident_id", "?"),
            "timestamp":   datetime.now(timezone.utc).isoformat(),
        })
    return path


def _draw_timeline(incident: dict) -> Path | None:
    """Blocking — runs in executor."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches

        events = incident.get("sub_events", [])[:15]
        if not events:
            return None

        fig, ax = plt.subplots(figsize=(16, max(4, len(events) * 0.6)),
                                facecolor="#07090f")
        ax.set_facecolor("#07090f")

        y_positions = list(range(len(events), 0, -1))
        colors = {
            "sysmon_event":    "#ff44aa",
            "etw_threat_event":"#ff6600",
            "canary_intrusion":"#ff2200",
            "dpi_alert":       "#4488ff",
            "ebpf_alert":      "#ff4400",
        }

        for i, (evt, y) in enumerate(zip(events, y_positions)):
            etype   = evt.get("type", "unknown")
            process = evt.get("process", evt.get("attacker_ip", "?"))
            tech    = evt.get("technique", "")
            color   = colors.get(etype, "#666666")

            ax.scatter([i], [y], s=200, c=color, zorder=5)
            ax.text(i, y + 0.3, f"{etype.upper()[:12]}", ha="center",
                    va="bottom", fontsize=7, color=color)
            ax.text(i, y - 0.3, process[:16], ha="center",
                    va="top", fontsize=6, color="rgba(255,255,255,0.6)")
            if tech:
                ax.text(i, y - 0.6, tech, ha="center",
                        va="top", fontsize=6, color="#ffaa44")

            if i > 0:
                ax.annotate("", xy=(i, y), xytext=(i-1, y_positions[i-1]),
                            arrowprops=dict(arrowstyle="->",
                                            color="#00ff4144", lw=1))

        ax.set_yticks([])
        ax.set_xticks(range(len(events)))
        ax.set_xticklabels([f"T+{i}" for i in range(len(events))],
                            color="#888888", fontsize=8)
        ax.set_title(
            f"Attack Timeline — INC-{incident.get('incident_id','?')} "
            f"[{incident.get('kill_chain_phase','?')}]",
            color="#ff8844", fontsize=12, pad=15,
        )
        ax.set_facecolor("#07090f")
        ax.tick_params(colors="#555555")
        for spine in ax.spines.values():
            spine.set_edgecolor("#1a1a2a")

        plt.tight_layout()
        path = _DIAGRAMS_DIR / f"timeline_{incident.get('incident_id','unk')}_{_ts()}.png"
        plt.savefig(str(path), dpi=110, bbox_inches="tight",
                    facecolor="#07090f")
        plt.close()
        logger.info(f"DIAGRAM: attack timeline → {path.name}")
        return path

    except Exception as e:
        logger.debug(f"DIAGRAM: timeline error: {e}")
        return None


async def generate_qr_code(
    data: str,
    label: str,
    broadcast_fn,
) -> Path | None:
    """
    Generate QR code for phishing simulation / payload delivery testing.
    data: URL or payload string to encode.
    """
    loop = asyncio.get_running_loop()
    path = await loop.run_in_executor(
        None, _draw_qr, data, label
    )
    if path:
        await broadcast_fn({
            "type":      "diagram_generated",
            "diagram":   "qr_code",
            "path":      str(path),
            "label":     label,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    return path


def _draw_qr(data: str, label: str) -> Path | None:
    try:
        import qrcode
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        qr  = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H)
        qr.add_data(data)
        qr.make(fit=True)
        img = qr.make_image(fill_color="#00ff41", back_color="#07090f")

        fig, ax = plt.subplots(figsize=(6, 7), facecolor="#07090f")
        ax.imshow(img, cmap="Greens")
        ax.set_title(f"JARVIS QR — {label[:40]}", color="#00ff41",
                     fontsize=10, pad=8)
        ax.axis("off")
        plt.tight_layout()

        path = _DIAGRAMS_DIR / f"qr_{label[:20].replace(' ','_')}_{_ts()}.png"
        plt.savefig(str(path), dpi=150, bbox_inches="tight",
                    facecolor="#07090f")
        plt.close()
        logger.info(f"DIAGRAM: QR code → {path.name}")
        return path
    except Exception as e:
        logger.debug(f"DIAGRAM: QR error: {e}")
        return None
