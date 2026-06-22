#!/usr/bin/env python3
"""Validate and stage hermes_cli/web_dist files into an offline overlay."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path


def _iter_web_dist_files(web_dist_dir: Path) -> list[Path]:
    return sorted(path for path in web_dist_dir.rglob("*") if path.is_file())


def _validate_web_dist(web_dist_dir: Path) -> list[Path]:
    index_html = web_dist_dir / "index.html"
    vite_manifest = web_dist_dir / ".vite" / "manifest.json"
    missing = [path for path in (index_html,) if not path.is_file()]
    if missing:
        missing_list = ", ".join(str(path) for path in missing)
        raise SystemExit(
            "web_dist build artifacts are missing. Run the web production build first. "
            f"Missing: {missing_list}"
        )

    files = _iter_web_dist_files(web_dist_dir)
    if not files:
        raise SystemExit(f"web_dist directory is empty: {web_dist_dir}")

    # Newer Hermes web builds may not emit Vite's .vite/manifest.json into
    # the final hermes_cli/web_dist directory. In that case, fall back to a
    # simpler presence check: index.html plus at least one asset file.
    if not vite_manifest.is_file():
        if not any(path.parent.name == "assets" for path in files):
            raise SystemExit(
                "web_dist is missing both the Vite manifest and any built assets. "
                f"Expected at least one file under {web_dist_dir / 'assets'}."
            )
        return files

    manifest = json.loads(vite_manifest.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or not manifest:
        raise SystemExit(f"Vite manifest is empty or invalid: {vite_manifest}")

    referenced_assets: set[Path] = set()
    for entry_name, entry in manifest.items():
        if not isinstance(entry, dict):
            raise SystemExit(f"Vite manifest entry must be an object: {entry_name}")
        file_name = entry.get("file")
        if isinstance(file_name, str) and file_name:
            referenced_assets.add(web_dist_dir / file_name)
        css_files = entry.get("css", [])
        if css_files is not None and not isinstance(css_files, list):
            raise SystemExit(f"Vite manifest css field must be a list: {entry_name}")
        for css_name in css_files or []:
            if isinstance(css_name, str) and css_name:
                referenced_assets.add(web_dist_dir / css_name)

    missing_assets = sorted(path for path in referenced_assets if not path.is_file())
    if missing_assets:
        missing_list = ", ".join(str(path) for path in missing_assets)
        raise SystemExit(
            "web_dist manifest references files that are not present. "
            f"Missing: {missing_list}"
        )

    return files


def stage_web_dist(repo_root: Path, bundle_dir: Path, manifest_path: Path) -> list[str]:
    web_dist_dir = repo_root / "hermes_cli" / "web_dist"
    files = _validate_web_dist(web_dist_dir)

    staged_paths: list[str] = []
    for src in files:
        rel = src.relative_to(repo_root).as_posix()
        dst = bundle_dir / "overlay" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        staged_paths.append(rel)

    with manifest_path.open("a", encoding="utf-8") as handle:
        for rel in staged_paths:
            handle.write(f"{rel}\n")

    return staged_paths


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(
            "Usage: web_dist_overlay.py <repo-root> <bundle-dir> <overlay-manifest>",
            file=sys.stderr,
        )
        return 1

    repo_root = Path(argv[1]).resolve()
    bundle_dir = Path(argv[2]).resolve()
    manifest_path = Path(argv[3]).resolve()
    staged = stage_web_dist(repo_root, bundle_dir, manifest_path)
    print(f"Staged {len(staged)} web_dist files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
