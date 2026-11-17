"""Out-of-process health checks never open the Telegram session."""

import json
import time
from pathlib import Path


def read_status(directory: Path) -> dict[str, object]:
    try:
        value = json.loads((directory / "health.json").read_text())
        if not isinstance(value, dict):
            raise ValueError
        tick = value["last_tick"]
        timeout = value["watchdog_timeout"]
        if not isinstance(tick, (int, float)) or not isinstance(timeout, (int, float)):
            raise ValueError
        value["healthy"] = (
            value.get("phase") not in {"stopped", "fatal"} and 0 <= time.time() - tick <= timeout
        )
        value["ready"] = value["healthy"] and value.get("ready") is True
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return {"healthy": False, "ready": False, "phase": "unavailable"}
