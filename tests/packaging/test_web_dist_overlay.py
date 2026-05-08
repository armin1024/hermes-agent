import json
import subprocess
import sys
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "packaging" / "offline" / "web_dist_overlay.py"


def _make_web_dist(repo_root: Path) -> Path:
    web_dist = repo_root / "hermes_cli" / "web_dist"
    (web_dist / ".vite").mkdir(parents=True, exist_ok=True)
    (web_dist / "assets").mkdir(parents=True, exist_ok=True)
    (web_dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    (web_dist / "assets" / "app-123.js").write_text("console.log('ok');", encoding="utf-8")
    (web_dist / "assets" / "app-123.css").write_text("body{}", encoding="utf-8")
    (web_dist / ".vite" / "manifest.json").write_text(
        json.dumps(
            {
                "index.html": {
                    "file": "assets/app-123.js",
                    "css": ["assets/app-123.css"],
                }
            }
        ),
        encoding="utf-8",
    )
    return web_dist


def test_stages_web_dist_files_and_updates_manifest(tmp_path):
    repo_root = tmp_path / "repo"
    bundle_dir = tmp_path / "bundle"
    manifest_path = bundle_dir / "overlay.manifest"
    (bundle_dir / "overlay").mkdir(parents=True)
    manifest_path.write_text("gateway/run.py\n", encoding="utf-8")
    _make_web_dist(repo_root)

    subprocess.run(
        [sys.executable, str(SCRIPT), str(repo_root), str(bundle_dir), str(manifest_path)],
        check=True,
    )

    assert (bundle_dir / "overlay" / "hermes_cli" / "web_dist" / "index.html").is_file()
    assert (bundle_dir / "overlay" / "hermes_cli" / "web_dist" / ".vite" / "manifest.json").is_file()
    assert (
        bundle_dir / "overlay" / "hermes_cli" / "web_dist" / "assets" / "app-123.js"
    ).is_file()
    manifest_lines = manifest_path.read_text(encoding="utf-8").splitlines()
    assert manifest_lines == [
        "gateway/run.py",
        "hermes_cli/web_dist/.vite/manifest.json",
        "hermes_cli/web_dist/assets/app-123.css",
        "hermes_cli/web_dist/assets/app-123.js",
        "hermes_cli/web_dist/index.html",
    ]


def test_fails_when_web_dist_sentinels_are_missing(tmp_path):
    repo_root = tmp_path / "repo"
    bundle_dir = tmp_path / "bundle"
    manifest_path = bundle_dir / "overlay.manifest"
    (bundle_dir / "overlay").mkdir(parents=True)
    manifest_path.write_text("", encoding="utf-8")
    (repo_root / "hermes_cli" / "web_dist").mkdir(parents=True)

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(repo_root), str(bundle_dir), str(manifest_path)],
        capture_output=True,
        text=True,
    )

    assert proc.returncode != 0
    assert "web_dist build artifacts are missing" in proc.stderr


def test_fails_when_manifest_references_missing_asset(tmp_path):
    repo_root = tmp_path / "repo"
    bundle_dir = tmp_path / "bundle"
    manifest_path = bundle_dir / "overlay.manifest"
    (bundle_dir / "overlay").mkdir(parents=True)
    manifest_path.write_text("", encoding="utf-8")
    web_dist = _make_web_dist(repo_root)
    (web_dist / "assets" / "app-123.js").unlink()

    proc = subprocess.run(
        [sys.executable, str(SCRIPT), str(repo_root), str(bundle_dir), str(manifest_path)],
        capture_output=True,
        text=True,
    )

    assert proc.returncode != 0
    assert "web_dist manifest references files that are not present" in proc.stderr
