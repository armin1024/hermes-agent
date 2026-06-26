"""AOPS-compatible skill uninstall helpers.

The AOPS channel exposes both local ``/skills uninstall`` commands and the
silent SkillHub bridge.  Hub-installed skills are tracked in the hub lock file,
but Tec01 may also ask to uninstall a local/profile skill that has no lock
entry.  This module contains the shared, conservative local fallback.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any


def is_not_hub_installed_message(message: str) -> bool:
    text = str(message or "").lower()
    return "not a hub-installed skill" in text or "is not a hub-installed skill" in text


def _skill_ref_key(value: Any) -> str:
    return str(value or "").strip().lower().replace("_", "-").lstrip("/")


def _safe_relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except Exception:
        return str(path)


def _invalidate_skill_caches() -> None:
    try:
        from agent.skill_commands import reload_skills

        reload_skills()
    except Exception:
        pass
    try:
        from agent.prompt_builder import clear_skills_system_prompt_cache

        clear_skills_system_prompt_cache(clear_snapshot=True)
    except Exception:
        pass


def _build_local_skill_items() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from agent.skill_commands import get_skill_commands, scan_skill_commands
    from agent.skill_utils import iter_skill_index_files
    from hermes_cli.config import load_config
    from hermes_cli.skills_config import get_disabled_skills
    from tools.skills_tool import SKILLS_DIR, _parse_frontmatter, skill_matches_platform

    skills_root = SKILLS_DIR
    items: list[dict[str, Any]] = []
    disabled = get_disabled_skills(load_config())
    command_by_path: dict[str, str] = {}
    command_by_name: dict[str, str] = {}
    try:
        commands = scan_skill_commands() or get_skill_commands()
        for command, info in commands.items():
            normalized_command = str(command or "").strip().lower().replace("_", "-")
            if not normalized_command.startswith("/"):
                normalized_command = f"/{normalized_command.lstrip('/')}"
            skill_md_path = str(info.get("skill_md_path") or "").strip()
            if skill_md_path:
                command_by_path[str(Path(skill_md_path).resolve())] = normalized_command
            name = str(info.get("name") or "").strip()
            if name:
                command_by_name[name] = normalized_command
    except Exception:
        command_by_path = {}
        command_by_name = {}

    if skills_root.exists():
        for skill_md in iter_skill_index_files(skills_root, "SKILL.md"):
            parts = set(skill_md.parts)
            if ".git" in parts or ".hub" in parts or "node_modules" in parts:
                continue
            try:
                content = skill_md.read_text(encoding="utf-8")
                frontmatter, _body = _parse_frontmatter(content)
            except Exception:
                frontmatter = {}
            try:
                if not skill_matches_platform(frontmatter):
                    continue
            except Exception:
                pass
            name = str(frontmatter.get("name") or skill_md.parent.name)
            item_id = _safe_relative(skill_md.parent, skills_root)
            command = (
                command_by_path.get(str(skill_md.resolve()))
                or command_by_name.get(name)
                or f"/{name.lower().replace(' ', '-').replace('_', '-')}".strip("/")
            )
            if command and not str(command).startswith("/"):
                command = f"/{command}"
            items.append(
                {
                    "id": item_id,
                    "name": name,
                    "enabled": name not in disabled,
                    "disabled": name in disabled,
                    "command": command,
                    "path": str(skill_md),
                }
            )
    items.sort(key=lambda item: (str(item.get("name") or "").lower(), str(item.get("id") or "")))
    return items, {"skillsRoot": str(skills_root)}


def _match_local_skill(target_ref: str, items: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    target_key = _skill_ref_key(target_ref)
    matches: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    for item in items:
        candidates = {
            _skill_ref_key(item.get("name")),
            _skill_ref_key(item.get("id")),
            _skill_ref_key(item.get("command")),
        }
        if target_key not in candidates:
            continue
        marker = str(item.get("path") or item.get("id") or item.get("name") or "")
        if marker in seen_paths:
            continue
        seen_paths.add(marker)
        matches.append(item)
    if len(matches) == 1:
        return matches[0], matches
    return None, matches


def _validate_local_skill_dir(matched: dict[str, Any], skills_root: Path) -> tuple[bool, str | None, Path | None]:
    raw_path = str(matched.get("path") or "").strip()
    if not raw_path:
        return False, "Matched skill has no SKILL.md path.", None
    skill_md = Path(raw_path)
    skill_dir = skill_md.parent

    try:
        root_resolved = skills_root.resolve()
        dir_resolved = skill_dir.resolve()
    except Exception as exc:
        return False, f"Cannot resolve skill path: {exc}", None

    if dir_resolved == root_resolved:
        return False, "Refusing to remove the skills root directory.", None
    try:
        if not dir_resolved.is_relative_to(root_resolved):
            return False, "Refusing to remove a path outside the current profile skills directory.", None
    except AttributeError:
        if not str(dir_resolved).startswith(str(root_resolved) + "/"):
            return False, "Refusing to remove a path outside the current profile skills directory.", None

    try:
        relative_parts = dir_resolved.relative_to(root_resolved).parts
    except Exception:
        relative_parts = ()
    if ".hub" in relative_parts:
        return False, "Refusing to remove hub metadata directories.", None

    cursor = skill_dir
    root_check = root_resolved
    checked: list[Path] = []
    try:
        while cursor.resolve() != root_check:
            checked.append(cursor)
            parent = cursor.parent
            if parent == cursor:
                break
            cursor = parent
    except Exception:
        checked = [skill_dir]
    for path in checked:
        if path.is_symlink():
            return False, f"Refusing to remove a symlinked skill path: {path}", None

    if not skill_dir.is_dir():
        return False, "Matched skill directory does not exist.", None
    if not (skill_dir / "SKILL.md").is_file():
        return False, "Matched directory does not contain SKILL.md.", None

    return True, None, skill_dir


def uninstall_local_skill_fallback(target_ref: str, *, items: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    from hermes_cli.config import load_config
    from hermes_cli.skills_config import get_disabled_skills, save_disabled_skills
    from tools.skills_tool import SKILLS_DIR

    target_ref = str(target_ref or "").strip()
    if not target_ref:
        return {
            "ok": False,
            "error": {"code": "SKILL_NAME_REQUIRED", "message": "Skill name is required."},
        }

    if items is None:
        try:
            items, context = _build_local_skill_items()
        except Exception as exc:
            return {
                "ok": False,
                "error": {"code": "SKILLS_READ_FAILED", "message": str(exc)},
            }
    else:
        context = {"skillsRoot": str(SKILLS_DIR)}

    matched, matches = _match_local_skill(target_ref, items)
    if matched is None:
        if matches:
            return {
                "ok": False,
                "error": {
                    "code": "SKILL_AMBIGUOUS",
                    "message": f"Skill `{target_ref}` matched multiple installed skills.",
                    "details": {
                        "matches": [
                            {
                                "name": item.get("name"),
                                "id": item.get("id"),
                                "command": item.get("command"),
                                "path": item.get("path"),
                            }
                            for item in matches
                        ]
                    },
                },
                "context": context,
            }
        return {
            "ok": False,
            "error": {"code": "SKILL_NOT_FOUND", "message": f"Skill `{target_ref}` not found."},
            "context": context,
        }

    valid, reason, skill_dir = _validate_local_skill_dir(matched, SKILLS_DIR)
    if not valid or skill_dir is None:
        return {
            "ok": False,
            "error": {
                "code": "SKILL_UNINSTALL_FAILED",
                "message": reason or f"Failed to uninstall `{target_ref}`.",
            },
            "context": context,
        }

    skill_name = str(matched.get("name") or skill_dir.name)
    removed_path = str(skill_dir)
    try:
        shutil.rmtree(skill_dir)
        cfg = load_config()
        disabled = get_disabled_skills(cfg)
        if skill_name in disabled:
            disabled.discard(skill_name)
            save_disabled_skills(cfg, disabled)
        _invalidate_skill_caches()
    except Exception as exc:
        return {
            "ok": False,
            "error": {"code": "SKILL_UNINSTALL_FAILED", "message": str(exc)},
            "context": context,
        }

    return {
        "ok": True,
        "name": skill_name,
        "source": "local",
        "removedPath": removed_path,
        "message": f"Uninstalled local skill '{skill_name}' from {removed_path}",
        "context": context,
    }
