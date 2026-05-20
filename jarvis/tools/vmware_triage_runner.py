#!/usr/bin/env python3
"""
tools/vmware_triage_runner.py — Standalone interactive VMware snapshot triage runner.

Usage: python tools/vmware_triage_runner.py <manifest.json>

Reads a PENDING_REVIEW triage manifest, shows entropy and MITRE detections,
prompts for manual confirmation, then calls vmrun.exe to take a clean snapshot
so the analyst can safely execute the payload in the guest OS manually.
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _load_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _display_summary(manifest: dict) -> None:
    triage = manifest.get("triage", {})
    bar = "═" * 62
    print(f"\n{bar}")
    print("  JARVIS FORENSIC TRIAGE RUNNER — MANIFEST REVIEW")
    print(bar)
    print(f"  UUID      : {manifest.get('uuid')}")
    print(f"  Timestamp : {manifest.get('timestamp')}")
    print(f"  Status    : {manifest.get('status')}")
    print(f"\n  Command   : {str(manifest.get('command', ''))[:120]}")
    entropy   = triage.get("entropy", "?")
    high_flag = "  ⚠ HIGH ENTROPY" if triage.get("high_entropy") else ""
    print(f"\n  Entropy   : {entropy}{high_flag}")
    detections = triage.get("mitre_detections", [])
    if detections:
        print(f"\n  MITRE Detections ({len(detections)}):")
        for d in detections:
            print(f"    [{d['technique']}] {d['name']}")
            for p in d.get("matched_patterns", []):
                print(f"      • {p}")
    else:
        print("\n  MITRE Detections : none")
    ips = triage.get("extracted_ips", [])
    if ips:
        print(f"\n  Extracted IPs    : {', '.join(ips)}")
    vmw = manifest.get("vmware", {})
    print(f"\n  VMX Path      : {vmw.get('vmx_path') or '(not set)'}")
    print(f"  Auto-approved : {vmw.get('auto_approved', False)}")
    print(f"{bar}\n")


def _take_snapshot(vmx_path: str, snapshot_name: str) -> bool:
    result = subprocess.run(
        ["vmrun.exe", "-T", "ws", "snapshot", vmx_path, snapshot_name],
        capture_output=True,
        text=True,
        shell=False,
    )
    if result.returncode == 0:
        print(f"  [OK] Snapshot '{snapshot_name}' created.")
        return True
    print(f"  [ERR] vmrun.exe exited {result.returncode}: {result.stderr.strip()}")
    return False


def _update_manifest(path: Path, manifest: dict, snapshot_name: str) -> None:
    manifest["vmware"]["snapshot_target"] = snapshot_name
    manifest["status"] = "SNAPSHOT_TAKEN"
    path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: python tools/vmware_triage_runner.py <manifest.json>")
        sys.exit(1)

    manifest_path = Path(sys.argv[1])
    if not manifest_path.exists():
        print(f"Error: manifest not found: {manifest_path}")
        sys.exit(1)

    manifest = _load_manifest(manifest_path)
    _display_summary(manifest)

    vmx_path = manifest.get("vmware", {}).get("vmx_path")
    if not vmx_path:
        vmx_path = input("  VMX path (blank to abort): ").strip()
        if not vmx_path:
            print("  Aborted.")
            sys.exit(0)
        manifest["vmware"]["vmx_path"] = vmx_path

    confirm = input(
        "  Authorize snapshot before manual guest execution? [y/N]: "
    ).strip().lower()
    if confirm != "y":
        print("  Not authorized. Exiting.")
        sys.exit(0)

    ts_tag        = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    snapshot_name = f"triage_{ts_tag}"
    if _take_snapshot(vmx_path, snapshot_name):
        _update_manifest(manifest_path, manifest, snapshot_name)
        print(f"  Manifest updated → status=SNAPSHOT_TAKEN, snapshot='{snapshot_name}'")
    else:
        print("  Snapshot failed. Manifest unchanged.")


if __name__ == "__main__":
    main()
