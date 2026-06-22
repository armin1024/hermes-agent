"""Small persistent AOPS channel state helpers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


def _state_path() -> Path:
    root = get_hermes_home() / "aops"
    root.mkdir(parents=True, exist_ok=True)
    return root / "channel-state.json"


def _safe_key_part(value: Any) -> str:
    text = str(value or "").strip() or "unknown"
    return re.sub(r"[^A-Za-z0-9_.:-]+", "_", text)[:160]


def preference_key(*, platform: str, channel_id: str, agent_key: str | None = None) -> str:
    return ":".join(
        [
            _safe_key_part(platform),
            _safe_key_part(channel_id),
            _safe_key_part(agent_key or "main"),
        ]
    )


def load_state() -> dict[str, Any]:
    path = _state_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict[str, Any]) -> Path:
    path = _state_path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def get_model_preference(key: str) -> dict[str, Any] | None:
    prefs = load_state().get("modelPreferences")
    if not isinstance(prefs, dict):
        return None
    value = prefs.get(key)
    return value if isinstance(value, dict) else None


def set_model_preference(key: str, preference: dict[str, Any]) -> Path:
    state = load_state()
    prefs = state.setdefault("modelPreferences", {})
    if not isinstance(prefs, dict):
        prefs = {}
        state["modelPreferences"] = prefs
    prefs[key] = preference
    return save_state(state)


def delete_model_preference(key: str) -> Path | None:
    state = load_state()
    prefs = state.get("modelPreferences")
    if not isinstance(prefs, dict) or key not in prefs:
        return None
    prefs.pop(key, None)
    return save_state(state)
