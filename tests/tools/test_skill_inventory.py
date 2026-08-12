from pathlib import Path

from tools.skill_inventory import classify_skill, inspect_skillhub_integrity
from tools.skills_guard import content_hash


def _write_skill(root: Path, name: str = "demo") -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\n---\n# Demo\n", encoding="utf-8")
    return skill


def _hub_entry(skill: Path, root: Path) -> dict:
    return {
        "source": "clawhub",
        "identifier": "demo",
        "install_path": skill.relative_to(root).as_posix(),
        "files": ["SKILL.md"],
        "content_hash": content_hash(skill),
        "installed_at": "2026-08-01T10:00:00+00:00",
        "updated_at": "2026-08-01T10:00:00+00:00",
    }


def test_skillhub_integrity_pristine_and_content_modified(tmp_path):
    root = tmp_path / "skills"
    skill = _write_skill(root)
    entry = _hub_entry(skill, root)

    pristine = inspect_skillhub_integrity(skill, root, entry)
    assert pristine["status"] == "pristine"

    (skill / "SKILL.md").write_text("changed\n", encoding="utf-8")
    modified = inspect_skillhub_integrity(skill, root, entry)
    assert modified["status"] == "modified"
    assert modified["reason"] == "content_changed"


def test_skillhub_integrity_added_deleted_and_runtime_noise(tmp_path):
    root = tmp_path / "skills"
    skill = _write_skill(root)
    entry = _hub_entry(skill, root)

    cache = skill / "__pycache__"
    cache.mkdir()
    (cache / "helper.cpython-311.pyc").write_bytes(b"cache")
    assert inspect_skillhub_integrity(skill, root, entry)["status"] == "pristine"

    (skill / "notes.md").write_text("new", encoding="utf-8")
    added = inspect_skillhub_integrity(skill, root, entry)
    assert added["reason"] == "files_added"

    (skill / "notes.md").unlink()
    (skill / "SKILL.md").unlink()
    deleted = inspect_skillhub_integrity(skill, root, entry)
    assert deleted["reason"] == "files_deleted"


def test_skillhub_integrity_symlink_and_incomplete_lock(tmp_path):
    root = tmp_path / "skills"
    skill = _write_skill(root)
    entry = _hub_entry(skill, root)
    (skill / "outside").symlink_to(tmp_path)
    result = inspect_skillhub_integrity(skill, root, entry)
    assert result["status"] == "modified"
    assert result["reason"] == "symlink_detected"

    incomplete = inspect_skillhub_integrity(skill, root, {"install_path": "demo"})
    assert incomplete["status"] == "unknown"
    assert incomplete["reason"] == "lock_incomplete"


def test_source_precedence_and_local_fallback(tmp_path):
    root = tmp_path / "skills"
    skill = _write_skill(root)
    entry = _hub_entry(skill, root)

    hub = classify_skill(
        "demo",
        skill,
        root,
        {
            "hub": {"demo": entry},
            "bundled": {"demo"},
            "usage": {"demo": {"created_by": "agent"}},
        },
    )
    assert hub["source"] == "skillhub"
    assert hub["modified"] is False

    builtin = classify_skill(
        "demo", skill, root,
        {"hub": {}, "bundled": {"demo"}, "usage": {"demo": {"created_by": "agent"}}},
    )
    assert builtin["source"] == "builtin"

    agent = classify_skill(
        "demo", skill, root,
        {"hub": {}, "bundled": set(), "usage": {"demo": {"created_by": "agent"}}},
    )
    assert agent["source"] == "agent_generated"

    legacy_agent = classify_skill(
        "demo", skill, root,
        {"hub": {}, "bundled": set(), "usage": {"demo": {"agent_created": True}}},
    )
    assert legacy_agent["source"] == "agent_generated"

    user = classify_skill(
        "demo", skill, root,
        {"hub": {}, "bundled": set(), "usage": {}},
    )
    assert user["source"] == "user_created"


def test_skillhub_source_matches_install_path_when_lock_key_differs(tmp_path):
    root = tmp_path / "skills"
    skill = _write_skill(root)
    entry = _hub_entry(skill, root)

    result = classify_skill(
        "demo",
        skill,
        root,
        {
            "hub": {"market-slug-that-differs": entry},
            "bundled": set(),
            "usage": {},
        },
    )

    assert result["source"] == "skillhub"
    assert result["modified"] is False
