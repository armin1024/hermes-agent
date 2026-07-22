"""Idempotently enable the AOPS PDF runtime capabilities in an existing profile."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import yaml

from utils import atomic_yaml_write

_AOPS_DEFAULT_TOOLSETS = (
    "terminal",
    "file",
    "code_execution",
    "skills",
    "todo",
    "memory",
    "session_search",
    "clarify",
    "delegation",
    "cronjob",
    "messaging",
    "vision",
)


def _mapping(parent: dict[str, Any], key: str, changed: list[str]) -> dict[str, Any]:
    value = parent.get(key)
    if isinstance(value, dict):
        return value
    value = {}
    parent[key] = value
    changed.append(key)
    return value


def _append_tools(
    toolsets: dict[str, Any], platform: str, required: tuple[str, ...], changed: list[str]
) -> None:
    value = toolsets.get(platform)
    if not isinstance(value, list):
        value = []
        toolsets[platform] = value
    added = False
    for tool in required:
        if tool not in value:
            value.append(tool)
            added = True
    if added:
        changed.append(f"platform_toolsets.{platform}")


def ensure_pdf_capabilities(config: dict[str, Any]) -> list[str]:
    """Mutate only PDF/vision capability switches; preserve model endpoint selection."""
    changed: list[str] = []

    model = _mapping(config, "model", changed)
    if model.get("supports_vision") is not True:
        model["supports_vision"] = True
        changed.append("model.supports_vision")

    platform_toolsets = _mapping(config, "platform_toolsets", changed)
    configured_platforms = [
        platform for platform in ("aops", "cli") if isinstance(platform_toolsets.get(platform), list)
    ]
    if configured_platforms:
        for platform in configured_platforms:
            _append_tools(platform_toolsets, platform, ("file", "vision"), changed)
    else:
        # AOPS resolves ``cli`` before the legacy ``aops`` list. Seed both with
        # the audited profile defaults when neither exists, so enabling vision
        # cannot accidentally reduce an old profile to only two toolsets.
        for platform in ("cli", "aops"):
            platform_toolsets[platform] = list(_AOPS_DEFAULT_TOOLSETS)
            changed.append(f"platform_toolsets.{platform}")

    aops = _mapping(config, "aops", changed)
    aops_toolsets = _mapping(aops, "toolsets", changed)
    disabled = aops_toolsets.get("disabled")
    if isinstance(disabled, list) and "vision" in disabled:
        aops_toolsets["disabled"] = [item for item in disabled if item != "vision"]
        changed.append("aops.toolsets.disabled")

    return changed


def apply_to_config(config_path: Path) -> dict[str, Any]:
    if not config_path.is_file():
        raise FileNotFoundError(f"Hermes profile config not found: {config_path}")
    loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Hermes profile config must contain a YAML mapping: {config_path}")
    changed = ensure_pdf_capabilities(loaded)
    if changed:
        atomic_yaml_write(config_path, loaded, sort_keys=False)
    model = loaded.get("model") if isinstance(loaded.get("model"), dict) else {}
    return {
        "ok": True,
        "changed": bool(changed),
        "changedPaths": changed,
        "configPath": str(config_path),
        "model": model.get("model") or model.get("default"),
        "provider": model.get("provider"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    config_path = args.config or Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser() / "config.yaml"
    try:
        result = apply_to_config(config_path)
    except Exception as exc:
        result = {"ok": False, "changed": False, "code": "pdf_setup_failed", "error": str(exc)}
        print(json.dumps(result, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
