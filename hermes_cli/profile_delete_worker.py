"""Detached worker for destructive AOPS profile deletion.

The gateway launches this module after sending the confirmation reply.  It
must be independent of the profile gateway because stopping that gateway can
terminate the process that requested the deletion.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path


def _root_for_profile(profile_home: Path) -> Path:
    resolved = profile_home.resolve()
    if resolved.parent.name == "profiles":
        return resolved.parent.parent
    return resolved


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Delete a Hermes named profile")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--profile-home", required=True)
    parser.add_argument("--operation-id", default="")
    parser.add_argument("--delay", type=float, default=1.5)
    args = parser.parse_args(argv)

    profile_home = Path(args.profile_home).resolve()
    root = _root_for_profile(profile_home)
    if profile_home.parent != root / "profiles" or args.profile.strip().lower() == "default":
        print("profile deletion rejected: only named profiles are supported", file=sys.stderr)
        return 2

    # Give the AOPS adapter time to flush the acceptance response before the
    # service is stopped.  The worker is detached, so the gateway may exit.
    if args.delay > 0:
        time.sleep(min(args.delay, 10.0))

    os.environ["HERMES_HOME"] = str(root)
    uid = getattr(os, "getuid", lambda: -1)()
    runtime_dir = Path(f"/run/user/{uid}") if uid >= 0 else None
    if runtime_dir is not None and runtime_dir.is_dir():
        os.environ.setdefault("XDG_RUNTIME_DIR", str(runtime_dir))
        os.environ.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={runtime_dir}/bus")

    try:
        from hermes_cli.profiles import delete_profile

        delete_profile(args.profile, yes=True)
        return 0
    except FileNotFoundError:
        # Idempotent completion: a concurrent cleanup already removed it.
        return 0 if not profile_home.exists() else 1
    except Exception as exc:
        print(f"profile deletion failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
