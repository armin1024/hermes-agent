#!/usr/bin/env python3
"""Extract a PDF with Hermes' bounded PyMuPDF pipeline.

Usage:
    python extract_pymupdf.py document.pdf
    python extract_pymupdf.py document.pdf --json
    python extract_pymupdf.py document.pdf --pages 0-4
    python extract_pymupdf.py document.pdf --dpi 180 --max-pages 50

The JSON result uses schema ``hermes.pdf-extraction.v1``. Pages with too
little text or meaningful graphics are rendered into the Hermes image cache;
pass those image paths to ``vision_analyze``.
"""

from __future__ import annotations

import argparse
import json
import sys

from tools.pdf_extraction import (
    DEFAULT_DPI,
    DEFAULT_MAX_PAGES,
    DEFAULT_MAX_RENDER_PAGES,
    DEFAULT_MAX_TOTAL_PIXELS,
    DEFAULT_TIMEOUT_SECONDS,
    PDF_RESULT_SCHEMA,
    PDFExtractionError,
    analyze_pdf,
)


def _page_indexes(value: str) -> set[int]:
    try:
        if "-" in value:
            start, end = (int(part) for part in value.split("-", 1))
            if start < 0 or end < start:
                raise ValueError
            return set(range(start, end + 1))
        index = int(value)
        if index < 0:
            raise ValueError
        return {index}
    except ValueError as exc:
        raise argparse.ArgumentTypeError("pages must be a zero-based index or range such as 0-4") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("--json", "--analyze", action="store_true", dest="json_output")
    parser.add_argument("--pages", type=_page_indexes)
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--max-render-pages", type=int, default=DEFAULT_MAX_RENDER_PAGES)
    parser.add_argument("--max-total-pixels", type=int, default=DEFAULT_MAX_TOTAL_PIXELS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = analyze_pdf(
            args.path,
            dpi=args.dpi,
            max_pages=args.max_pages,
            max_render_pages=args.max_render_pages,
            max_total_pixels=args.max_total_pixels,
            timeout_seconds=args.timeout,
        )
    except PDFExtractionError as exc:
        print(json.dumps({
            "schema_version": PDF_RESULT_SCHEMA,
            "error": str(exc),
            "code": exc.code,
        }, ensure_ascii=False))
        return 2

    if args.pages is not None:
        result["pages"] = [page for page in result["pages"] if page["page"] - 1 in args.pages]
        result["selected_pages"] = sorted(args.pages)
    if args.json_output:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    for page in result["pages"]:
        print(f"\n--- Page {page['page']}/{result['page_count']} [{page['mode']}] ---\n")
        if page["text"]:
            print(page["text"])
        if page.get("rendered_image"):
            print(f"[Use vision_analyze on: {page['rendered_image']}]")
        elif page.get("render_skipped"):
            print(f"[Page rendering skipped: {page['render_skipped']}]")
    for warning in result["warnings"]:
        print(f"WARNING: {warning}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
