"""AOPS-specific user-facing internationalization.

Hermes' shared catalog intentionally covers only a thin set of gateway
messages.  AOPS has a much larger command protocol, so its catalog is kept in
two dedicated files:

``locales/aops_en.yaml`` and ``locales/aops_zh.yaml``.

Language selection still uses :mod:`agent.i18n` (``HERMES_LANGUAGE`` then
``display.language``).  Chinese variants resolve to Simplified Chinese; all
other languages use the English AOPS baseline until a reviewed translation is
added.
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

import yaml

from agent.i18n import _locales_dir, get_language

logger = logging.getLogger(__name__)

_catalogs: dict[str, dict[str, str]] = {}
_lock = threading.Lock()


def _flatten(node: Any, prefix: str = "") -> dict[str, str]:
    result: dict[str, str] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            child = f"{prefix}.{key}" if prefix else str(key)
            result.update(_flatten(value, child))
    elif isinstance(node, str):
        result[prefix] = node
    return result


def _catalog_language() -> str:
    return "zh" if get_language() in {"zh", "zh-hant"} else "en"


def _load_catalog(language: str) -> dict[str, str]:
    with _lock:
        cached = _catalogs.get(language)
        if cached is not None:
            return cached
    path = Path(_locales_dir()) / f"aops_{language}.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        catalog = _flatten(raw)
    except Exception as exc:
        logger.warning("Failed to load AOPS i18n catalog %s: %s", path, exc)
        catalog = {}
    with _lock:
        _catalogs[language] = catalog
    return catalog


def reset_aops_language_cache() -> None:
    """Clear AOPS catalogs after a runtime language configuration change."""
    with _lock:
        _catalogs.clear()


def aops_t(key: str, **values: Any) -> str:
    """Return one AOPS user-facing message in the active language."""
    language = _catalog_language()
    value = _load_catalog(language).get(key)
    if value is None and language != "en":
        value = _load_catalog("en").get(key)
    if value is None:
        logger.debug("AOPS i18n miss: key=%r language=%r", key, language)
        value = key
    if values:
        try:
            return value.format(**values)
        except (KeyError, IndexError, ValueError) as exc:
            logger.warning(
                "AOPS i18n format failed key=%r language=%r values=%r: %s",
                key,
                language,
                values,
                exc,
            )
    return value


def aops_error(
    code: str,
    key: str,
    *,
    raw_message: str | None = None,
    details: dict[str, Any] | None = None,
    **values: Any,
) -> dict[str, Any]:
    """Build a localized error while retaining the original diagnostic."""
    localized = aops_t(key, **values)
    result_details = dict(details or {})
    raw = str(raw_message or "").strip()
    if raw and raw != localized:
        result_details["rawMessage"] = raw
    result: dict[str, Any] = {"code": code, "message": localized}
    if result_details:
        result["details"] = result_details
    return result


__all__ = ["aops_error", "aops_t", "reset_aops_language_cache"]
