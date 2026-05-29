"""
core/config_manager.py — Unified configuration (v46.0).

One YAML file replaces 30+ scattered env vars.
File: jarvis_config.yaml in JARVIS root.
Hot-reload on file change. Sensible defaults. Validation.

Priority: YAML > env vars > defaults.

Generate default config: jarvis --init-config
"""

from pathlib import Path
from loguru import logger

_CONFIG_PATH = Path("jarvis_config.yaml")
_config:       dict = {}
_loaded_mtime: float = 0.0


DEFAULT_CONFIG = """# JARVIS v46.0 Configuration
# Generated automatically. Edit freely.
# All settings hot-reload on file save.

# ── Models ──────────────────────────────────────────────────────────
models:
  fast:    qwen2.5:7b-instruct-q5_K_M
  deep:    qwen2.5:14b-instruct-q4_K_M
  vision:  moondream:latest

# ── Ollama ──────────────────────────────────────────────────────────
ollama:
  host:           "127.0.0.1"
  port:           11434
  parallel:       1
  keep_alive:     "30m"

# ── Detection ───────────────────────────────────────────────────────
detection:
  canary_ports:        [21, 2222, 8445, 3389, 1433]
  tarpit_ports:        [4444, 5900, 8080, 9200, 27017]
  correlator_window_s: 60

# ── Optional subsystems ─────────────────────────────────────────────
optional:
  screen_monitor:   false
  proxy_intel:      false
  hunt_scheduler:   true
  telegram_bridge:  true

# ── Telegram (set token via env var or here) ────────────────────────
telegram:
  token:       ""
  chat_id:     0

# ── API keys (or use env vars) ──────────────────────────────────────
api_keys:
  shodan:      ""
  virustotal:  ""
  otx:         ""
  github:      ""

# ── Lab targets ─────────────────────────────────────────────────────
lab:
  default_target: "192.168.1.100"
  vmrun_path:     "C:\\\\Program Files (x86)\\\\VMware\\\\VMware Workstation\\\\vmrun.exe"
  kali_host:      ""
  kali_user:      "kali"
  kali_key_path:  ""

# ── Behavior ────────────────────────────────────────────────────────
behavior:
  quiet_boot:        false
  personality:       true
  daily_briefing:    true
  session_journal:   true

# ── Performance ─────────────────────────────────────────────────────
performance:
  whisper_compute_type: "int8"
  llm_context_max:      4096
  embedding_cache_size: 512
"""


def load_config() -> dict:
    """Load config from YAML. Generate defaults if missing."""
    global _config, _loaded_mtime

    if not _CONFIG_PATH.exists():
        _CONFIG_PATH.write_text(DEFAULT_CONFIG, encoding="utf-8")
        logger.info(
            f"CONFIG: created default config at {_CONFIG_PATH}"
        )

    try:
        import yaml
        mtime = _CONFIG_PATH.stat().st_mtime
        if mtime == _loaded_mtime:
            return _config

        _config       = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
        _loaded_mtime = mtime
        logger.info(f"CONFIG: loaded {_CONFIG_PATH}")
        return _config

    except Exception as e:
        logger.error(f"CONFIG: load error: {e}")
        return _config


def get(key_path: str, default=None):
    """
    Get a config value by dotted path.
    Example: get("models.fast") or get("optional.hunt_scheduler", True)
    Priority: YAML > env var > default.
    """
    import os

    load_config()   # auto-reload check

    parts = key_path.split(".")
    cur   = _config
    for p in parts:
        if isinstance(cur, dict) and p in cur:
            cur = cur[p]
        else:
            cur = None
            break

    if cur is not None and cur != "":
        return cur

    # Fallback: env var (uppercased dotted path)
    env_key = "JARVIS_" + key_path.upper().replace(".", "_")
    val     = os.getenv(env_key, "")
    if val:
        # Coerce types
        if val.lower() in ("true", "yes", "1"):  return True
        if val.lower() in ("false", "no", "0"): return False
        try:
            return int(val)
        except ValueError:
            pass
        try:
            return float(val)
        except ValueError:
            pass
        return val

    return default


def get_all() -> dict:
    """Return full config dict."""
    load_config()
    return dict(_config)


def reload() -> dict:
    """Force reload from disk."""
    global _loaded_mtime
    _loaded_mtime = 0.0
    return load_config()
