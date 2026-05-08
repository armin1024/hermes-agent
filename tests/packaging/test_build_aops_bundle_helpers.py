import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFY_SCRIPT = REPO_ROOT / "packaging" / "offline" / "verify_overlay_imports.py"
README_SCRIPT = REPO_ROOT / "packaging" / "offline" / "render_bundle_readme.py"


def _write_overlay_cron(bundle_dir: Path, manifest_lines: list[str]) -> Path:
    overlay_root = bundle_dir / "overlay" / "cron"
    overlay_root.mkdir(parents=True, exist_ok=True)
    (overlay_root / "__init__.py").write_text(
        "from .jobs import get_job_history\n",
        encoding="utf-8",
    )
    (overlay_root / "jobs.py").write_text(
        "def record_job_history(*args, **kwargs):\n    return None\n"
        "def query_cron_history(*args, **kwargs):\n    return []\n"
        "def get_job_history(*args, **kwargs):\n    return []\n",
        encoding="utf-8",
    )
    (overlay_root / "scheduler.py").write_text("# ok\n", encoding="utf-8")
    manifest_path = bundle_dir / "overlay.manifest"
    manifest_path.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
    return manifest_path


def test_verify_overlay_imports_accepts_consistent_cron_overlay(tmp_path):
    repo_root = tmp_path / "repo"
    bundle_dir = tmp_path / "bundle"
    manifest_path = _write_overlay_cron(
        bundle_dir,
        ["cron/__init__.py", "cron/jobs.py", "cron/scheduler.py"],
    )

    proc = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), str(repo_root), str(bundle_dir), str(manifest_path)],
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr


def test_verify_overlay_imports_fails_when_manifest_misses_cron_file(tmp_path):
    repo_root = tmp_path / "repo"
    bundle_dir = tmp_path / "bundle"
    manifest_path = _write_overlay_cron(
        bundle_dir,
        ["cron/__init__.py", "cron/scheduler.py"],
    )

    proc = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), str(repo_root), str(bundle_dir), str(manifest_path)],
        capture_output=True,
        text=True,
    )

    assert proc.returncode != 0
    assert "cron/jobs.py" in proc.stderr


def test_render_bundle_readme_replaces_new_placeholders(tmp_path):
    template = tmp_path / "README.template.md"
    output = tmp_path / "README.md"
    template.write_text(
        "__BUNDLE_ARCHIVE__\n__BUNDLE_DIR__\n__VERSION__\n__GIT_SHA__\n",
        encoding="utf-8",
    )

    subprocess.run(
        [
            sys.executable,
            str(README_SCRIPT),
            str(template),
            str(output),
            "bundle.tar.gz",
            "offline-bundle-v0.11.0-aops-deadbeef",
            "0.11.0",
            "deadbeef",
        ],
        check=True,
    )

    assert output.read_text(encoding="utf-8") == (
        "bundle.tar.gz\noffline-bundle-v0.11.0-aops-deadbeef\n0.11.0\ndeadbeef\n"
    )
