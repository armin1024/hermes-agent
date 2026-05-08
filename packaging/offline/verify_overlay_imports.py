#!/usr/bin/env python3
"""Validate that the bundle overlay contains a consistent cron package."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path


REQUIRED_FILES = (
    "cron/__init__.py",
    "cron/jobs.py",
    "cron/scheduler.py",
)


def _die(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        return _die("usage: verify_overlay_imports.py <repo-root> <bundle-dir> <manifest-path>")

    repo_root = Path(argv[1]).resolve()
    bundle_dir = Path(argv[2]).resolve()
    manifest_path = Path(argv[3]).resolve()
    overlay_root = bundle_dir / "overlay"

    manifest_entries = {
        line.strip()
        for line in manifest_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    missing = [path for path in REQUIRED_FILES if path not in manifest_entries]
    if missing:
        return _die(f"overlay.manifest is missing required cron files: {', '.join(missing)}")

    sys.path.insert(0, str(overlay_root))
    sys.path.insert(1, str(repo_root))

    cron_pkg = importlib.import_module("cron")
    cron_jobs = importlib.import_module("cron.jobs")

    cron_pkg_path = Path(getattr(cron_pkg, "__file__", "")).resolve()
    cron_jobs_path = Path(getattr(cron_jobs, "__file__", "")).resolve()
    if not str(cron_pkg_path).startswith(str(overlay_root)):
        return _die(f"cron package resolved outside overlay: {cron_pkg_path}")
    if not str(cron_jobs_path).startswith(str(overlay_root)):
        return _die(f"cron.jobs resolved outside overlay: {cron_jobs_path}")

    for attr_name in ("record_job_history", "query_cron_history"):
        if not hasattr(cron_jobs, attr_name):
            return _die(f"cron.jobs is missing required attribute: {attr_name}")
    if not hasattr(cron_pkg, "get_job_history"):
        return _die("cron package is missing required attribute: get_job_history")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
