# JARVIS Plugin: threat_escalator v0.1
# Escalates severity to 10.0 for events with concurrent injection + exfil TTPs.
# To activate: compute sha256 of this file, add entry to plugins/manifest.json:
#   {"name":"threat_escalator","file":"threat_escalator.example.py",
#    "sha256":"<hash>","version":"0.1","enabled":true}
#
# analyze(event:dict) -> dict or None
#
# V69 M61.7 — THIS PLUGIN IS NOT EXECUTED. Dynamic source-code plugin execution
# is disabled: the previous 'sandbox' (an exec globals dict with trimmed
# __builtins__) was not a privilege boundary, and the manifest SHA-256 is not a
# signature. The file is kept as a worked example of the analyze() contract.
# See core/plugin_loader.py for the migration path.

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
