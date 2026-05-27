"""
tools/ghost_hands.py — GUI lab automation engine (v40.0).

Automates opening, positioning, and configuring tools for
cybersecurity lab environments using pyautogui + pygetwindow.

SAFETY:
  - pyautogui.FAILSAFE = True (ALWAYS) — move mouse to (0,0) to stop
  - Hard 30s timeout per action sequence
  - All actions logged before execution
"""

import asyncio, os, subprocess, time
from pathlib import Path
from loguru import logger

_PROFILES_PATH = Path(__file__).parent / "lab_profiles.yaml"

import pyautogui
pyautogui.FAILSAFE = True
pyautogui.PAUSE    = 0.3


def _load_profiles() -> dict:
    try:
        import yaml
        return yaml.safe_load(_PROFILES_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.debug(f"GHOST_HANDS: profile load error: {e}")
        return {}


async def execute_lab_profile(
    profile_name: str,
    broadcast_fn,
    tts=None,
) -> bool:
    """
    Execute a named lab profile.
    Opens tools, waits for them to load, organizes windows.
    Hard 30s timeout per step.
    """
    profiles = _load_profiles()
    profile  = profiles.get("profiles", {}).get(profile_name)

    if not profile:
        logger.warning(f"GHOST_HANDS: unknown profile '{profile_name}'")
        available = list(profiles.get("profiles", {}).keys())
        if tts:
            asyncio.create_task(tts.speak_async(
                f"Profile '{profile_name}' not found. "
                f"Available: {', '.join(available)}"
            ))
        return False

    logger.info(f"GHOST_HANDS: executing profile '{profile_name}'")

    await broadcast_fn({
        "type":    "ghost_hands_started",
        "profile": profile_name,
        "steps":   len(profile.get("steps", [])),
        "timestamp": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
    })

    if tts:
        asyncio.create_task(tts.speak_async(
            f"Preparing {profile.get('description', profile_name)}."
        ))

    loop = asyncio.get_running_loop()

    for i, step in enumerate(profile.get("steps", [])):
        action = step.get("action", "")
        logger.info(f"GHOST_HANDS: step {i+1} — {action}")

        try:
            await asyncio.wait_for(
                loop.run_in_executor(None, _execute_step, step),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.error(f"GHOST_HANDS: step '{action}' timed out (30s)")
            await broadcast_fn({
                "type":    "ghost_hands_timeout",
                "step":    action,
                "profile": profile_name,
            })
            break
        except Exception as e:
            logger.debug(f"GHOST_HANDS: step error: {e}")
            continue

    await broadcast_fn({
        "type":    "ghost_hands_complete",
        "profile": profile_name,
        "timestamp": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc
        ).isoformat(),
    })

    if tts:
        asyncio.create_task(tts.speak_async(
            f"{profile.get('description', profile_name)} ready."
        ))

    return True


def _execute_step(step: dict) -> None:
    """Execute a single automation step. Runs in executor (blocking)."""
    action = step.get("action", "")

    if action == "open_app":
        path = step.get("path", "")
        if path and Path(path).exists():
            subprocess.Popen(
                [path] + step.get("args", []),
                creationflags=subprocess.CREATE_NO_WINDOW
                if os.name == "nt" else 0,
            )
            time.sleep(step.get("wait_seconds", 3))

    elif action == "open_url":
        import webbrowser
        webbrowser.open(step.get("url", ""))
        time.sleep(2)

    elif action == "type_text":
        time.sleep(step.get("delay_before", 0.5))
        pyautogui.write(step.get("text", ""), interval=0.05)

    elif action == "hotkey":
        keys = step.get("keys", [])
        if keys:
            pyautogui.hotkey(*keys)
        time.sleep(step.get("delay_after", 0.5))

    elif action == "wait":
        time.sleep(step.get("seconds", 2))

    elif action == "arrange_windows":
        _arrange_windows(step.get("layout", "side_by_side"))


def _arrange_windows(layout: str) -> None:
    """Arrange open windows in a predefined layout."""
    try:
        import pygetwindow as gw
        import pyautogui
        screen_w, screen_h = pyautogui.size()
        windows = [w for w in gw.getAllWindows()
                   if w.title and w.isActive or True][:4]

        if layout == "side_by_side" and len(windows) >= 2:
            half_w = screen_w // 2
            for i, win in enumerate(windows[:2]):
                try:
                    win.moveTo(i * half_w, 30)
                    win.resizeTo(half_w, screen_h - 60)
                except Exception:
                    pass

        elif layout == "quad" and len(windows) >= 4:
            half_w = screen_w // 2
            half_h = (screen_h - 60) // 2
            positions = [(0,30), (half_w,30), (0,30+half_h), (half_w,30+half_h)]
            for win, (x, y) in zip(windows[:4], positions):
                try:
                    win.moveTo(x, y)
                    win.resizeTo(half_w, half_h)
                except Exception:
                    pass

    except Exception as e:
        logger.debug(f"GHOST_HANDS: window arrange error: {e}")


def list_profiles() -> list[str]:
    profiles = _load_profiles()
    return list(profiles.get("profiles", {}).keys())
