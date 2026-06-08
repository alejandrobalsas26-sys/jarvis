# JARVIS Plugin: threat_escalator v0.1
# Escalates severity to 10.0 for events with concurrent injection + exfil TTPs.
# To activate: compute sha256 of this file, add entry to plugins/manifest.json:
#   {"name":"threat_escalator","file":"threat_escalator.example.py",
#    "sha256":"<hash>","version":"0.1","enabled":true}
#
# analyze(event:dict) -> dict or None
# Sandbox: re, json, time, hashlib available; no file/network/os/subprocess.

def analyze(event):
    sev = float(event.get("severity", 0) or 0)
    attck = list(event.get("attck") or [])
    injection = any(str(t).upper().startswith("T1055") for t in attck)
    exfil = any(str(t).upper() in ("T1048", "T1041") for t in attck)
    if sev >= 9.0 and injection and exfil:
        ev = dict(event)
        ev["severity"] = 10.0
        ev["plugin_note"] = "threat_escalator: concurrent injection+exfil — critical escalation"
        return ev
    return None
