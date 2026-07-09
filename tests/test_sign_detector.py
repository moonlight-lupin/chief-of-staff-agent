#!/usr/bin/env python3
"""Tests for sign_detector.py — PDF and DOCX signature detection."""

import sys
import json
import tempfile
from pathlib import Path

import pytest

SIGN_SCRIPTS = Path(__file__).resolve().parents[1] / "skills" / "self-sign" / "scripts"
if str(SIGN_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SIGN_SCRIPTS))

try:
    import fitz  # pymupdf
    HAS_PYMUPDF = True
except ImportError:
    HAS_PYMUPDF = False

try:
    from docx import Document
    HAS_PYTHON_DOCX = True
except ImportError:
    HAS_PYTHON_DOCX = False


def create_test_docx(path, paragraphs):
    """Helper: create a DOCX with the given paragraph texts."""
    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    doc.save(str(path))


def create_test_pdf(path, lines):
    """Helper: create a PDF with the given text lines."""
    doc = fitz.open()
    page = doc.new_page()
    y = 72
    for line in lines:
        page.insert_text((72, y), line, fontsize=11)
        y += 20
    doc.save(str(path))
    doc.close()


@pytest.mark.skipif(not HAS_PYTHON_DOCX, reason="python-docx not installed")
class TestDOCXDetection:
    def test_detects_signature_line(self, tmp_path):
        from sign_detector import detect_docx
        docx_path = tmp_path / "test.docx"
        create_test_docx(docx_path, [
            "Service Agreement",
            "",
            "For and on behalf of the Service Provider:",
            "Signature: ____________________",
            "Date: ____________________",
        ])
        locations = detect_docx(docx_path)
        assert len(locations) >= 1
        # Should find the signature line
        sig_locs = [l for l in locations if l.location_type == "signature"]
        assert len(sig_locs) >= 1

    def test_detects_date_field(self, tmp_path):
        from sign_detector import detect_docx
        docx_path = tmp_path / "test.docx"
        create_test_docx(docx_path, [
            "Signature: ____________________",
            "Date: ____________________",
        ])
        locations = detect_docx(docx_path)
        date_locs = [l for l in locations if l.location_type == "date"]
        assert len(date_locs) >= 1

    def test_detects_party_context(self, tmp_path):
        from sign_detector import detect_docx
        docx_path = tmp_path / "test.docx"
        create_test_docx(docx_path, [
            "For and on behalf of the Client:",
            "Signature: ____________________",
            "",
            "For and on behalf of the Service Provider:",
            "Signature: ____________________",
        ])
        locations = detect_docx(docx_path)
        # At least 2 signature locations
        sig_locs = [l for l in locations if l.location_type == "signature"]
        assert len(sig_locs) >= 2

    def test_classifies_self_party(self, tmp_path):
        from sign_detector import detect_docx, _classify_party
        docx_path = tmp_path / "test.docx"
        create_test_docx(docx_path, [
            "For and on behalf of the Service Provider:",
            "Signature: ____________________",
        ])
        locations = detect_docx(docx_path)
        sig = [l for l in locations if l.location_type == "signature"][0]
        classification = _classify_party(sig.party_context, "Test Co", ["Service Provider", "Consultant"])
        assert classification == "self"

    def test_classifies_other_party(self, tmp_path):
        from sign_detector import detect_docx, _classify_party
        docx_path = tmp_path / "test.docx"
        create_test_docx(docx_path, [
            "For and on behalf of the Client:",
            "Signature: ____________________",
        ])
        locations = detect_docx(docx_path)
        sig = [l for l in locations if l.location_type == "signature"][0]
        classification = _classify_party(sig.party_context, "Test Co", ["Service Provider"])
        assert classification == "other"

    def test_no_signatures_found(self, tmp_path):
        from sign_detector import detect_docx
        docx_path = tmp_path / "test.docx"
        create_test_docx(docx_path, [
            "This is a regular document with no signature blocks.",
            "Just some text here.",
        ])
        locations = detect_docx(docx_path)
        assert len(locations) == 0

    def test_underscore_run_detection(self, tmp_path):
        from sign_detector import detect_docx
        docx_path = tmp_path / "test.docx"
        create_test_docx(docx_path, [
            "Please sign here: __________________________",
        ])
        locations = detect_docx(docx_path)
        assert len(locations) >= 1


@pytest.mark.skipif(not HAS_PYMUPDF, reason="pymupdf not installed")
class TestPDFDetection:
    def test_detects_pdf_signature(self, tmp_path):
        from sign_detector import detect_pdf
        pdf_path = tmp_path / "test.pdf"
        create_test_pdf(pdf_path, [
            "Service Agreement",
            "",
            "For and on behalf of the Service Provider:",
            "Signature: ____________________",
            "Date: ____________________",
        ])
        locations = detect_pdf(pdf_path)
        assert len(locations) >= 1
        sig_locs = [l for l in locations if l.location_type == "signature"]
        assert len(sig_locs) >= 1

    def test_pdf_party_context(self, tmp_path):
        from sign_detector import detect_pdf
        pdf_path = tmp_path / "test.pdf"
        create_test_pdf(pdf_path, [
            "For and on behalf of the Client:",
            "Signature: ____________________",
            "",
            "For and on behalf of the Service Provider:",
            "Signature: ____________________",
        ])
        locations = detect_pdf(pdf_path)
        sig_locs = [l for l in locations if l.location_type == "signature"]
        assert len(sig_locs) >= 2

    def test_pdf_multi_page(self, tmp_path):
        from sign_detector import detect_pdf
        pdf_path = tmp_path / "test.pdf"
        doc = fitz.open()
        p1 = doc.new_page()
        p1.insert_text((72, 72), "Page 1 content", fontsize=11)
        p2 = doc.new_page()
        p2.insert_text((72, 72), "Signature: ____________________", fontsize=11)
        doc.save(str(pdf_path))
        doc.close()
        locations = detect_pdf(pdf_path)
        assert len(locations) >= 1
        assert locations[0].page == 2  # 1-indexed page 2


class TestDetectSignaturesDispatch:
    def test_detect_signatures_auto_pdf(self, tmp_path):
        if not HAS_PYMUPDF:
            pytest.skip("pymupdf not installed")
        from sign_detector import detect_signatures
        pdf_path = tmp_path / "test.pdf"
        create_test_pdf(pdf_path, ["Signature: ____________________"])
        locations = detect_signatures(pdf_path, doc_type="auto")
        assert len(locations) >= 1

    def test_detect_signatures_auto_docx(self, tmp_path):
        if not HAS_PYTHON_DOCX:
            pytest.skip("python-docx not installed")
        from sign_detector import detect_signatures
        docx_path = tmp_path / "test.docx"
        create_test_docx(docx_path, ["Signature: ____________________"])
        locations = detect_signatures(docx_path, doc_type="auto")
        assert len(locations) >= 1