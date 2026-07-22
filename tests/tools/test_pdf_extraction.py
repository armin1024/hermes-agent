import json
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")

from tools.file_tools import read_file_tool
from tools.pdf_extraction import PDFExtractionError, analyze_pdf


def _build_pdf(path: Path) -> None:
    doc = pymupdf.open()
    text_page = doc.new_page()
    text_page.insert_text((72, 72), "Hermes normal PDF text " * 5)

    doc.new_page()  # empty/scan-like page

    mixed_page = doc.new_page()
    mixed_page.insert_text((72, 72), "Hermes mixed PDF text " * 5)
    pix = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 300, 300), 0)
    pix.clear_with(0xFF0000)
    mixed_page.insert_image((72, 120, 420, 500), pixmap=pix)
    doc.save(path)
    doc.close()


def test_analyze_pdf_extracts_text_and_renders_visual_pages(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    pdf = tmp_path / "mixed.pdf"
    _build_pdf(pdf)

    result = analyze_pdf(pdf)

    assert result["schema_version"] == "hermes.pdf-extraction.v1"
    assert result["page_count"] == 3
    assert result["mode"] == "mixed"
    assert result["pages"][0]["mode"] == "text"
    assert result["pages"][1]["mode"] == "visual"
    assert result["pages"][2]["mode"] == "mixed"
    rendered = [Path(page["rendered_image"]) for page in result["pages"] if page.get("rendered_image")]
    assert len(rendered) == 2
    assert all(path.is_file() and path.parent.name == "images" for path in rendered)


def test_analyze_pdf_enforces_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    pdf = tmp_path / "limited.pdf"
    _build_pdf(pdf)

    result = analyze_pdf(pdf, max_pages=1)
    assert result["processed_pages"] == 1
    assert result["truncated"] is True
    assert result["warnings"]

    with pytest.raises(PDFExtractionError) as error:
        analyze_pdf(pdf, max_file_bytes=1)
    assert error.value.code == "file_too_large"


def test_analyze_pdf_rejects_corrupt_and_encrypted_files(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    corrupt = tmp_path / "corrupt.pdf"
    corrupt.write_bytes(b"not a pdf")
    with pytest.raises(PDFExtractionError) as error:
        analyze_pdf(corrupt)
    assert error.value.code == "invalid_pdf"

    source = pymupdf.open()
    source.new_page().insert_text((72, 72), "secret")
    encrypted = tmp_path / "encrypted.pdf"
    source.save(
        encrypted,
        encryption=pymupdf.PDF_ENCRYPT_AES_256,
        owner_pw="owner",
        user_pw="user",
    )
    source.close()
    with pytest.raises(PDFExtractionError) as error:
        analyze_pdf(encrypted)
    assert error.value.code == "password_required"


def test_read_file_returns_page_text_and_vision_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    pdf = tmp_path / "attachment.pdf"
    _build_pdf(pdf)

    result = json.loads(read_file_tool(str(pdf), task_id="pdf-test"))

    assert result["extracted_document"] is True
    assert result["document_type"] == "pdf"
    assert result["pdf"]["mode"] == "mixed"
    assert len(result["pdf"]["rendered_pages"]) == 2
    assert "Page 1/3" in result["content"]
    assert "vision_analyze" in result["vision_hint"]
