#!/usr/bin/env python3
"""Render the AOPS offline bundle README from a template."""

from __future__ import annotations

import sys
from pathlib import Path


def _die(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    if len(argv) != 7:
        return _die(
            "usage: render_bundle_readme.py <template> <output> <bundle-archive> <bundle-dir> <version> <content-id>"
        )

    template_path = Path(argv[1]).resolve()
    output_path = Path(argv[2]).resolve()
    bundle_archive = argv[3]
    bundle_dir = argv[4]
    version = argv[5]
    content_id = argv[6]

    template = template_path.read_text(encoding="utf-8")
    rendered = (
        template.replace("__BUNDLE_ARCHIVE__", bundle_archive)
        .replace("__BUNDLE_DIR__", bundle_dir)
        .replace("__VERSION__", version)
        .replace("__CONTENT_ID__", content_id)
    )
    output_path.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
