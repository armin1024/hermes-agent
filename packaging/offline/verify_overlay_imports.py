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
    "utils.py",
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
    utils_mod = importlib.import_module("utils")

    cron_pkg_path = Path(getattr(cron_pkg, "__file__", "")).resolve()
    cron_jobs_path = Path(getattr(cron_jobs, "__file__", "")).resolve()
    utils_path = Path(getattr(utils_mod, "__file__", "")).resolve()
    if not str(cron_pkg_path).startswith(str(overlay_root)):
        return _die(f"cron package resolved outside overlay: {cron_pkg_path}")
    if not str(cron_jobs_path).startswith(str(overlay_root)):
        return _die(f"cron.jobs resolved outside overlay: {cron_jobs_path}")
    if not str(utils_path).startswith(str(overlay_root)):
        return _die(f"utils module resolved outside overlay: {utils_path}")
    for attr_name in (
        "atomic_json_write",
        "atomic_replace",
        "atomic_yaml_write",
        "base_url_host_matches",
        "base_url_hostname",
        "env_var_enabled",
        "is_truthy_value",
        "normalize_proxy_url",
    ):
        if not hasattr(utils_mod, attr_name):
            return _die(f"utils module is missing required attribute: {attr_name}")

    for attr_name in (
        "advance_next_run",
        "append_cron_history",
        "get_due_jobs",
        "mark_job_run",
        "save_job_output",
    ):
        if not hasattr(cron_jobs, attr_name):
            return _die(f"cron.jobs is missing required attribute: {attr_name}")
    for attr_name in ("list_jobs", "tick"):
        if not hasattr(cron_pkg, attr_name):
            return _die(f"cron package is missing required attribute: {attr_name}")

    # Import a representative slice of the AOPS/gateway runtime to catch
    # overlay/base version skew before the bundle reaches an offline host.
    for module_name in (
        "gateway.config",
        "gateway.platforms.base",
        "gateway.platforms.aops",
        "gateway.aops_commands",
        "gateway.run",
        "hermes_cli.gateway",
        "hermes_cli.setup",
        "hermes_cli.status",
        "hermes_cli.tools_config",
        "tools.send_message_tool",
        "tools.skills_hub",
        "agent.prompt_builder",
        "toolsets",
    ):
        module = importlib.import_module(module_name)
        module_path = Path(getattr(module, "__file__", "")).resolve()
        if not str(module_path).startswith(str(overlay_root)):
            return _die(f"{module_name} resolved outside overlay: {module_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
