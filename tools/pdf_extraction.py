"""Bounded PDF text extraction and page rendering for Hermes.

This module deliberately is not a separate model tool. ``read_file`` uses it
so PDF support does not add another schema to every model request.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_dir

PDF_RESULT_SCHEMA = "hermes.pdf-extraction.v1"
DEFAULT_DPI = 180
DEFAULT_MAX_FILE_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_PAGES = 100
DEFAULT_MAX_RENDER_PAGES = 20
DEFAULT_MAX_TOTAL_PIXELS = 60_000_000
DEFAULT_TIMEOUT_SECONDS = 60.0
MIN_TEXT_CHARS = 40
MIN_IMAGE_COVERAGE = 0.20
MIN_VECTOR_DRAWINGS = 20


class PDFExtractionError(Exception):
    """Raised when a PDF cannot be safely inspected."""

    def __init__(self, message: str, *, code: str = "pdf_extraction_failed") -> None:
        super().__init__(message)
        self.code = code


def _load_pymupdf():
    try:
        import pymupdf
    except ImportError as exc:
        raise PDFExtractionError(
            "PDF parsing requires PyMuPDF. Install the AOPS offline bundle that "
            "includes PyMuPDF, or install PyMuPDF==1.26.0 in the Hermes venv.",
            code="dependency_missing",
        ) from exc
    return pymupdf


def _image_coverage(page: Any) -> tuple[int, float]:
    """Return meaningful image count and approximate page-area coverage."""
    page_area = max(float(page.rect.width) * float(page.rect.height), 1.0)
    count = 0
    covered = 0.0
    try:
        infos = page.get_image_info(xrefs=True)
    except Exception:
        infos = []
    for info in infos:
        bbox = info.get("bbox") if isinstance(info, dict) else None
        if not bbox or len(bbox) != 4:
            continue
        width = max(float(bbox[2]) - float(bbox[0]), 0.0)
        height = max(float(bbox[3]) - float(bbox[1]), 0.0)
        area = width * height
        if area / page_area < 0.01:
            continue
        count += 1
        covered += area
    return count, min(covered / page_area, 1.0)


def _drawing_count(page: Any) -> int:
    try:
        return len(page.get_drawings())
    except Exception:
        return 0


def _cache_image_path(pdf_path: Path, page_number: int, dpi: int) -> Path:
    stat = pdf_path.stat()
    fingerprint = hashlib.sha256(
        f"{pdf_path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{dpi}".encode("utf-8")
    ).hexdigest()[:16]
    cache_dir = get_hermes_dir("cache/images", "image_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"pdf_{fingerprint}_page_{page_number:04d}_{dpi}dpi.png"


def _render_page(
    pymupdf: Any,
    page: Any,
    pdf_path: Path,
    page_number: int,
    dpi: int,
) -> tuple[Path, int]:
    scale = dpi / 72.0
    width = max(1, int(round(float(page.rect.width) * scale)))
    height = max(1, int(round(float(page.rect.height) * scale)))
    pixels = width * height
    output_path = _cache_image_path(pdf_path, page_number, dpi)
    if not output_path.is_file():
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(scale, scale),
            colorspace=pymupdf.csRGB,
            alpha=False,
        )
        pixmap.save(str(output_path))
    os.utime(output_path, None)
    return output_path, pixels


def analyze_pdf(
    path: str | os.PathLike[str],
    *,
    dpi: int = DEFAULT_DPI,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    max_pages: int = DEFAULT_MAX_PAGES,
    max_render_pages: int = DEFAULT_MAX_RENDER_PAGES,
    max_total_pixels: int = DEFAULT_MAX_TOTAL_PIXELS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Extract page text and render pages that need visual understanding."""
    pdf_path = Path(path).expanduser().resolve()
    if not pdf_path.is_file():
        raise PDFExtractionError(f"PDF file not found: {pdf_path}", code="not_found")
    if pdf_path.suffix.lower() != ".pdf":
        raise PDFExtractionError(f"Not a PDF file: {pdf_path}", code="unsupported_type")
    file_size = pdf_path.stat().st_size
    if file_size > max_file_bytes:
        raise PDFExtractionError(
            f"PDF is {file_size} bytes; limit is {max_file_bytes} bytes.",
            code="file_too_large",
        )
    if not 72 <= dpi <= 300:
        raise PDFExtractionError("PDF render DPI must be between 72 and 300.", code="invalid_limits")
    if min(max_pages, max_render_pages, max_total_pixels) <= 0 or timeout_seconds <= 0:
        raise PDFExtractionError("PDF safety limits must be positive.", code="invalid_limits")

    pymupdf = _load_pymupdf()
    started = time.monotonic()
    warnings: list[str] = []
    pages: list[dict[str, Any]] = []
    rendered_count = 0
    rendered_pixels = 0

    try:
        doc = pymupdf.open(str(pdf_path))
    except Exception as exc:
        raise PDFExtractionError(f"Cannot open PDF: {exc}", code="invalid_pdf") from exc

    try:
        if getattr(doc, "needs_pass", False):
            raise PDFExtractionError(
                "PDF is password-protected; provide a decrypted copy.",
                code="password_required",
            )
        page_count = len(doc)
        processed_pages = min(page_count, max_pages)
        if page_count > max_pages:
            warnings.append(f"Only the first {max_pages} of {page_count} pages were processed.")

        for index in range(processed_pages):
            if time.monotonic() - started > timeout_seconds:
                warnings.append(f"Processing stopped after the {timeout_seconds:g}s timeout.")
                break
            page = doc[index]
            try:
                text = page.get_text("text").strip()
            except Exception as exc:
                text = ""
                warnings.append(f"Page {index + 1}: text extraction failed: {exc}")
            image_count, image_coverage = _image_coverage(page)
            drawings = _drawing_count(page)
            low_text = len(text) < MIN_TEXT_CHARS
            visually_complex = image_coverage >= MIN_IMAGE_COVERAGE or drawings >= MIN_VECTOR_DRAWINGS
            if low_text:
                mode = "visual"
                render_reason = "insufficient_text"
            elif visually_complex:
                mode = "mixed"
                render_reason = "page_contains_graphics"
            else:
                mode = "text"
                render_reason = ""

            page_result: dict[str, Any] = {
                "page": index + 1,
                "mode": mode,
                "text": text,
                "text_chars": len(text),
                "image_count": image_count,
                "image_coverage": round(image_coverage, 4),
                "drawing_count": drawings,
            }
            if mode != "text":
                if rendered_count >= max_render_pages:
                    page_result["render_skipped"] = "render_page_limit"
                else:
                    scale = dpi / 72.0
                    projected_pixels = max(1, int(page.rect.width * scale)) * max(
                        1, int(page.rect.height * scale)
                    )
                    if rendered_pixels + projected_pixels > max_total_pixels:
                        page_result["render_skipped"] = "total_pixel_limit"
                    else:
                        try:
                            rendered_path, pixels = _render_page(
                                pymupdf, page, pdf_path, index + 1, dpi
                            )
                        except Exception as exc:
                            page_result["render_skipped"] = "render_failed"
                            warnings.append(f"Page {index + 1}: rendering failed: {exc}")
                        else:
                            rendered_count += 1
                            rendered_pixels += pixels
                            page_result["rendered_image"] = str(rendered_path)
                            page_result["render_reason"] = render_reason
                            page_result["rendered_pixels"] = pixels
            pages.append(page_result)
    finally:
        doc.close()

    modes = {page["mode"] for page in pages}
    if not modes:
        overall_mode = "empty"
    elif modes == {"text"}:
        overall_mode = "text"
    elif modes == {"visual"}:
        overall_mode = "visual"
    else:
        overall_mode = "mixed"
    actual_processed = len(pages)
    return {
        "schema_version": PDF_RESULT_SCHEMA,
        "path": str(pdf_path),
        "file_size": file_size,
        "page_count": page_count,
        "processed_pages": actual_processed,
        "truncated": actual_processed < page_count,
        "mode": overall_mode,
        "dpi": dpi,
        "rendered_pages": rendered_count,
        "rendered_pixels": rendered_pixels,
        "pages": pages,
        "warnings": warnings,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path")
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--max-render-pages", type=int, default=DEFAULT_MAX_RENDER_PAGES)
    parser.add_argument("--max-total-pixels", type=int, default=DEFAULT_MAX_TOTAL_PIXELS)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--json", action="store_true", help="Emit structured JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
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
        print(json.dumps({"schema_version": PDF_RESULT_SCHEMA, "error": str(exc), "code": exc.code}, ensure_ascii=False))
        return 2
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    for page in result["pages"]:
        print(f"\n--- Page {page['page']}/{result['page_count']} [{page['mode']}] ---\n")
        if page["text"]:
            print(page["text"])
        if page.get("rendered_image"):
            print(f"[Rendered page image: {page['rendered_image']}]")
    for warning in result["warnings"]:
        print(f"WARNING: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
