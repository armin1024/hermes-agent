"""Local skill source classification and SkillHub integrity inspection.

This module is intentionally network-free.  AOPS skill listings must remain
fast and reliable in restricted networks, so source data comes exclusively
from the profile's bundled manifest, usage sidecar, and SkillHub lock file.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from tools import skill_usage
from tools.skills_hub import HubLockFile


SOURCE_USER_CREATED = "user_created"
SOURCE_AGENT_GENERATED = "agent_generated"
SOURCE_SKILLHUB = "skillhub"
SOURCE_BUILTIN = "builtin"
SKILL_SOURCES = frozenset({
    SOURCE_USER_CREATED,
    SOURCE_AGENT_GENERATED,
    SOURCE_SKILLHUB,
    SOURCE_BUILTIN,
})


def _iso_to_ms(value: Any) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def load_source_context() -> dict[str, Any]:
    """Load one profile-scoped classification snapshot."""
    try:
        raw_installed = HubLockFile().load().get("installed", {})
    except Exception:
        raw_installed = {}
    installed = raw_installed if isinstance(raw_installed, dict) else {}
    return {
        "hub": {
            str(name): entry
            for name, entry in installed.items()
            if isinstance(entry, dict)
        },
        "bundled": skill_usage._read_bundled_manifest_names(),
        "usage": skill_usage.load_usage(),
    }


def _safe_lock_path(value: Any) -> PurePosixPath | None:
    if not isinstance(value, str) or not value.strip():
        return None
    path = PurePosixPath(value.strip())
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path


def _hub_entry_for_skill(
    name: str,
    skill_dir: Path,
    skills_root: Path,
    hub_entries: Any,
) -> dict[str, Any] | None:
    """Resolve a Hub record by name first, then by its authoritative path.

    Older/custom SkillHub registries can use a market slug as the lock key
    while the installed ``SKILL.md`` exposes a different public ``name``.
    Treating only the lock key as provenance made those valid installs fall
    through to ``user_created``.  ``install_path`` is profile-local and is the
    stable identity for that compatibility case.
    """
    if not isinstance(hub_entries, dict):
        return None
    exact = hub_entries.get(name)
    if isinstance(exact, dict):
        return exact
    try:
        relative = PurePosixPath(skill_dir.relative_to(skills_root).as_posix())
    except ValueError:
        return None
    for entry in hub_entries.values():
        if isinstance(entry, dict) and _safe_lock_path(entry.get("install_path")) == relative:
            return entry
    return None


def _is_generated_noise(path: str, expected_files: set[str]) -> bool:
    if path in expected_files:
        return False
    pure = PurePosixPath(path)
    return (
        "__pycache__" in pure.parts
        or pure.suffix == ".pyc"
        or pure.name == ".DS_Store"
    )


def _hash_files(skill_dir: Path, relative_files: list[str]) -> str:
    digest = hashlib.sha256()
    for rel in sorted(relative_files):
        digest.update(rel.encode("utf-8"))
        digest.update(b"\x00")
        digest.update((skill_dir / rel).read_bytes())
    return f"sha256:{digest.hexdigest()[:16]}"


def inspect_skillhub_integrity(
    skill_dir: Path,
    skills_root: Path,
    lock_entry: dict[str, Any],
) -> dict[str, Any]:
    """Compare a SkillHub directory with its install-time lock snapshot."""
    checked_at_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    installed_hash = str(lock_entry.get("content_hash") or "").strip() or None
    expected_raw = lock_entry.get("files")
    expected_files = {
        str(item).replace("\\", "/")
        for item in expected_raw
        if isinstance(item, str) and item.strip()
    } if isinstance(expected_raw, list) else set()

    base = {
        "installedHash": installed_hash,
        "currentHash": None,
        "checkedAtMs": checked_at_ms,
    }
    if not installed_hash or not expected_files:
        return {"status": "unknown", "reason": "lock_incomplete", **base}

    lock_path = _safe_lock_path(lock_entry.get("install_path"))
    try:
        root_resolved = skills_root.resolve()
        if skill_dir.is_symlink():
            return {"status": "modified", "reason": "symlink_detected", **base}
        skill_resolved = skill_dir.resolve()
        actual_lock_path = PurePosixPath(skill_resolved.relative_to(root_resolved).as_posix())
    except (OSError, ValueError):
        return {"status": "unknown", "reason": "lock_incomplete", **base}
    if lock_path is None or lock_path != actual_lock_path:
        return {"status": "unknown", "reason": "lock_incomplete", **base}

    actual_files: list[str] = []
    try:
        for entry in skill_dir.rglob("*"):
            rel = entry.relative_to(skill_dir).as_posix()
            if entry.is_symlink():
                return {"status": "modified", "reason": "symlink_detected", **base}
            if entry.is_file() and not _is_generated_noise(rel, expected_files):
                actual_files.append(rel)
        current_hash = _hash_files(skill_dir, actual_files)
    except OSError:
        return {"status": "unknown", "reason": "read_failed", **base}

    base["currentHash"] = current_hash
    actual_set = set(actual_files)
    if expected_files - actual_set:
        return {"status": "modified", "reason": "files_deleted", **base}
    if actual_set - expected_files:
        return {"status": "modified", "reason": "files_added", **base}
    if current_hash != installed_hash:
        return {"status": "modified", "reason": "content_changed", **base}
    return {"status": "pristine", "reason": None, **base}


def classify_skill(
    name: str,
    skill_dir: Path,
    skills_root: Path,
    context: dict[str, Any],
) -> dict[str, Any]:
    """Return the stable AOPS source and optional SkillHub integrity fields."""
    hub_entry = _hub_entry_for_skill(
        name,
        skill_dir,
        skills_root,
        context.get("hub", {}),
    )
    usage_record = context.get("usage", {}).get(name)
    usage_record = usage_record if isinstance(usage_record, dict) else {}

    if isinstance(hub_entry, dict):
        integrity = inspect_skillhub_integrity(skill_dir, skills_root, hub_entry)
        return {
            "source": SOURCE_SKILLHUB,
            "modified": (
                True if integrity["status"] == "modified"
                else False if integrity["status"] == "pristine"
                else None
            ),
            "sourceMetadata": {
                "market": hub_entry.get("source"),
                "identifier": hub_entry.get("identifier"),
                "installedAtMs": _iso_to_ms(hub_entry.get("installed_at")),
                "updatedAtMs": _iso_to_ms(hub_entry.get("updated_at")),
            },
            "integrity": integrity,
        }
    if name in context.get("bundled", set()):
        return {
            "source": SOURCE_BUILTIN,
            "modified": None,
            "sourceMetadata": {"creationTrigger": "bundled"},
            "integrity": {"status": "not_applicable"},
        }
    if skill_usage._is_curator_managed_record(usage_record):
        return {
            "source": SOURCE_AGENT_GENERATED,
            "modified": None,
            "sourceMetadata": {
                "creationTrigger": usage_record.get("creation_origin") or "background_review",
            },
            "integrity": {"status": "not_applicable"},
        }
    return {
        "source": SOURCE_USER_CREATED,
        "modified": None,
        "sourceMetadata": {
            "creationTrigger": usage_record.get("creation_origin") or "user_or_local",
        },
        "integrity": {"status": "not_applicable"},
    }
