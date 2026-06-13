"""
core/lab_manager.py — Autonomous VMware lab manager (v37.0).

Controls VMware Workstation via vmrun CLI.
Extends the existing forensic_volatility snapshot integration
with full VM lifecycle management.

Voice commands via macro system:
  "jarvis isolate victim"   → suspend victim VM network adapters
  "jarvis snapshot victim"  → take named snapshot
  "jarvis restore victim"   → restore to last clean snapshot
  "jarvis start victim"     → power on victim VM
  "jarvis list vms"         → broadcast list of all VMs
"""

import asyncio, os, subprocess
from datetime import datetime, timezone
from loguru import logger

_VMRUN = os.getenv("VMRUN_PATH", r"C:\Program Files (x86)\VMware\VMware Workstation\vmrun.exe")
_VM_REGISTRY: dict[str, str] = {}   # name → vmx_path

# Load from env vars: JARVIS_VM_victim="path/to/victim.vmx"
def _load_vm_registry() -> None:
    for key, val in os.environ.items():
        if key.startswith("JARVIS_VM_"):
            name = key[len("JARVIS_VM_"):].lower()
            _VM_REGISTRY[name] = val
    if _VM_REGISTRY:
        logger.info(f"LAB_MANAGER: registered VMs: {list(_VM_REGISTRY.keys())}")

_load_vm_registry()


def _vmrun(*args, timeout: int = 30) -> tuple[bool, str]:
    """Run vmrun command. Returns (success, output)."""
    cmd = [_VMRUN] + list(args)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
        return result.returncode == 0, result.stdout + result.stderr
    except FileNotFoundError:
        return False, "vmrun not found"
    except Exception as e:
        return False, str(e)


async def list_vms(broadcast_fn) -> list[str]:
    """List all running VMs."""
    loop = asyncio.get_running_loop()
    ok, output = await loop.run_in_executor(
        None, lambda: _vmrun("list")
    )
    vms = [l.strip() for l in output.splitlines()
           if l.strip() and l.strip() != "Total running VMs: 0"
           and "Total running VMs" not in l]

    await broadcast_fn({
        "type":   "lab_vm_list",
        "vms":    vms,
        "count":  len(vms),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return vms


async def isolate_vm(vm_name: str, broadcast_fn) -> bool:
    """
    Isolate a VM by disconnecting its network adapters.
    Useful for containing a compromised VM.
    """
    vmx = _VM_REGISTRY.get(vm_name.lower(), "")
    if not vmx:
        await broadcast_fn({
            "type":  "lab_error",
            "error": f"VM '{vm_name}' not in registry. "
                     "Set JARVIS_VM_{name} env var.",
        })
        return False

    loop  = asyncio.get_running_loop()

    # Disconnect network adapters via vmrun
    for adapter in ["ethernet0", "ethernet1"]:
        await loop.run_in_executor(
            None, lambda a=adapter: _vmrun(
                "setGuestNetworkAdapter", vmx,
                "-setDisconnected", a
            )
        )

    logger.warning(f"LAB_MANAGER: VM '{vm_name}' isolated — network disconnected")
    await broadcast_fn({
        "type":     "lab_vm_isolated",
        "vm_name":  vm_name,
        "severity": "HIGH",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return True


async def snapshot_vm(
    vm_name: str, snap_name: str, broadcast_fn
) -> bool:
    """Take a named snapshot of a VM."""
    vmx = _VM_REGISTRY.get(vm_name.lower(), "")
    if not vmx:
        return False

    loop = asyncio.get_running_loop()
    ok, out = await loop.run_in_executor(
        None, lambda: _vmrun("snapshot", vmx, snap_name, timeout=120)
    )
    logger.info(f"LAB_MANAGER: snapshot '{snap_name}' of '{vm_name}': {ok}")
    await broadcast_fn({
        "type":      "lab_snapshot_taken",
        "vm_name":   vm_name,
        "snap_name": snap_name,
        "success":   ok,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return ok


async def restore_vm(
    vm_name: str, snap_name: str, broadcast_fn
) -> bool:
    """Restore VM to a named snapshot."""
    vmx = _VM_REGISTRY.get(vm_name.lower(), "")
    if not vmx:
        return False

    loop = asyncio.get_running_loop()
    # Power off first
    await loop.run_in_executor(
        None, lambda: _vmrun("stop", vmx, "hard")
    )
    ok, out = await loop.run_in_executor(
        None, lambda: _vmrun(
            "revertToSnapshot", vmx, snap_name, timeout=120
        )
    )
    if ok:
        await loop.run_in_executor(
            None, lambda: _vmrun("start", vmx, "nogui")
        )
    logger.info(f"LAB_MANAGER: restore '{vm_name}' to '{snap_name}': {ok}")
    await broadcast_fn({
        "type":      "lab_vm_restored",
        "vm_name":   vm_name,
        "snap_name": snap_name,
        "success":   ok,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    return ok
