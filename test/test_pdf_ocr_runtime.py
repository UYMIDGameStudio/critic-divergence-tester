"""PDF extraction, bounded native rasterization, and local OCR deployment checks."""
import builtins
from contextlib import nullcontext
import hashlib
import os
from pathlib import Path
import struct
from subprocess import CompletedProcess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zlib

import document_review_ingest as ingest
import document_review_pdf_render as rendering


def pdf_fixture(pages=(None, "Native middle page", None), *, width=300, height=100):
    """Actual PDF objects/xref: vector-only pages surround a text page."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in pages:
        number = len(objects) + 1
        kids.append(f"{number} 0 R")
        content = (b"0 0 0 rg 10 10 100 50 re f" if text is None else
                   b"BT /F1 12 Tf 20 60 Td (" + text.encode("ascii") + b") Tj ET")
        objects.extend([
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Resources << /Font << /F1 3 0 R >> >> /Contents {number + 1} 0 R >>".encode(),
            f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
        ])
    objects[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>".encode()
    data = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(data))
        data.extend(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")
    startxref = len(data)
    data.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    data.extend("".join(f"{offset:010d} 00000 n \n" for offset in offsets).encode())
    data.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{startxref}\n%%EOF\n".encode())
    return bytes(data)


def parse_png(data):
    """Check CRCs and decompress actual pixels, with no imaging library."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("not a PNG")
    offset, idat, header = 8, bytearray(), None
    while offset < len(data):
        size = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        body = data[offset + 8:offset + 8 + size]
        crc = struct.unpack(">I", data[offset + 8 + size:offset + 12 + size])[0]
        if crc != zlib.crc32(kind + body) & 0xFFFFFFFF:
            raise AssertionError("invalid PNG checksum")
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        if kind == b"IDAT":
            idat.extend(body)
        offset += size + 12
    return header, zlib.decompress(idat)


class PDFOCRRuntimeTests(unittest.TestCase):
    def native_pdfium(self):
        try:
            import pypdfium2
        except (ImportError, OSError):
            self.skipTest("optional PDFium native renderer is not installed")
        return pypdfium2

    def test_native_gray_png_without_pillow_numpy_or_fitz(self):
        self.native_pdfium()
        original_import = builtins.__import__

        def import_without_optional(name, *args, **kwargs):
            if name.split(".")[0] in {"PIL", "numpy", "fitz"}:
                raise ImportError("excluded from portable package")
            return original_import(name, *args, **kwargs)

        data = pdf_fixture((None,))
        digest = hashlib.sha256(data).hexdigest()
        with patch("builtins.__import__", side_effect=import_without_optional):
            with rendering.open_pdf_renderer(data) as renderer:
                self.assertEqual(renderer.name, "pypdfium2")
                header, pixels = parse_png(renderer.render_page(1))
        self.assertEqual(header, (600, 200, 8, 0, 0, 0, 0))
        self.assertEqual(len(pixels), 200 * 601)
        self.assertIn(0, pixels)
        self.assertIn(255, pixels)
        self.assertEqual(hashlib.sha256(data).hexdigest(), digest)

    def test_real_mixed_pdf_ocr_remains_in_page_order_and_keeps_original_binding(self):
        self.native_pdfium()
        if ingest._pdf_backend() is None:
            self.skipTest("optional text backend is not installed")
        calls = []

        class OCR:
            name, version = "controlled-ocr", "test-1"
            def available(self): return True, "ready"
            def recognize_pdf_page(self, image, *, page_number, language):
                header, pixels = parse_png(image)
                if header[:2] != (1250, 417):
                    raise AssertionError("OCR raster did not use the validated 300 DPI resolution")
                calls.append(page_number)
                self_check = len(pixels) == (header[0] + 1) * header[1]
                if not self_check:
                    raise AssertionError("truncated image")
                return {"text": f"Scanned page {page_number}", "confidence": 98,
                        "low_confidence_words": 0, "language": language}

        data = pdf_fixture()
        document = ingest.ingest_bytes("mixed.pdf", data, ocr=OCR(), ocr_language="eng")
        self.assertEqual(calls, [1, 3])
        self.assertEqual([block.location.page for block in document.blocks], [1, 2, 3])
        self.assertEqual([entry["page"] for entry in document.source_to_block], [1, 2, 3])
        self.assertEqual([block.text for block in document.blocks], ["Scanned page 1", "Native middle page", "Scanned page 3"])
        self.assertEqual(document.source.sha256, hashlib.sha256(data).hexdigest())
        self.assertEqual(document.source.byte_size, len(data))
        self.assertEqual(document.quality.blank_pages, [])
        self.assertEqual(document.metadata["ocr"]["renderer"], "pypdfium2")
        self.assertTrue(document.metadata["ocr"]["temporary_grayscale"])
        self.assertEqual(document.metadata["ocr"]["render_dpi"], 300)

    def test_native_oversized_page_is_rejected_before_ocr_and_without_large_allocation(self):
        self.native_pdfium()
        with rendering.open_pdf_renderer(pdf_fixture((None,), width=100000, height=100000)) as renderer:
            with self.assertRaisesRegex(rendering.PDFRenderError, "上限"):
                renderer.render_page(1)
        with self.assertRaisesRegex(rendering.PDFRenderError, "页数"):
            with rendering.open_pdf_renderer(pdf_fixture(), max_pages=2):
                self.fail("too many pages were accepted")

    def test_native_ocr_failure_closes_document_and_is_not_relabelled_render_failure(self):
        pdfium = self.native_pdfium()
        native = pdfium.PdfDocument(pdf_fixture((None,)))
        with patch.object(pdfium, "PdfDocument", return_value=native):
            with self.assertRaisesRegex(ingest.IngestionError, "recognizer failed"):
                with rendering.open_pdf_renderer(pdf_fixture((None,))) as renderer:
                    renderer.render_page(1)
                    raise ingest.IngestionError("recognizer failed")
        self.assertTrue(native.raw is None)

    def test_pdfium_native_objects_close_when_png_encoding_fails(self):
        bitmap = SimpleNamespace(width=2, height=2, stride=2, buffer=b"\x00" * 4, n_channels=1, close=Mock())
        page = SimpleNamespace(get_size=lambda: (1, 1), render=Mock(return_value=bitmap), close=Mock())
        class Document:
            close = Mock()
            def __len__(self): return 1
            def __getitem__(self, index): return page
        document = Document()
        module = SimpleNamespace(PdfDocument=lambda data: document)
        with patch.object(rendering, "renderer_backend", return_value=("pypdfium2", module)), patch.object(rendering, "_gray_png", side_effect=RuntimeError("bad bitmap")):
            with self.assertRaises(rendering.PDFRenderError):
                with rendering.open_pdf_renderer(b"test") as renderer:
                    renderer.render_page(1)
        bitmap.close.assert_called_once()
        page.close.assert_called_once()
        document.close.assert_called_once()

    def test_renderer_limits_are_checked_before_rendering_and_native_page_is_closed(self):
        page = SimpleNamespace(get_size=lambda: (50000, 50000), render=Mock(), close=Mock())
        class Document:
            close = Mock()
            def __len__(self): return 1
            def __getitem__(self, index): return page
        document = Document()
        with patch.object(rendering, "renderer_backend", return_value=("pypdfium2", SimpleNamespace(PdfDocument=lambda data: document))):
            with self.assertRaises(rendering.PDFRenderError):
                with rendering.open_pdf_renderer(b"test") as renderer:
                    renderer.render_page(1)
        page.render.assert_not_called()
        page.close.assert_called_once()
        document.close.assert_called_once()

    def test_pymupdf_text_keeps_span_and_line_boundaries_and_closes_document(self):
        page = SimpleNamespace(get_text=Mock(return_value={"blocks": [{"type": 0, "bbox": (0, 0, 100, 100), "lines": [
            {"spans": [{"text": "pub"}, {"text": "lic"}]}, {"spans": [{"text": "policy"}]},
        ]}]}))
        class Document:
            is_encrypted, page_count = False, 1
            close = Mock()
            def __iter__(self): return iter([page])
        document = Document()
        module = SimpleNamespace(open=lambda **kwargs: document, TEXTFLAGS_DICT=255, TEXT_PRESERVE_IMAGES=4)
        data = pdf_fixture(("public",))
        with patch.object(ingest, "_pdf_backend", return_value=("pymupdf", module)):
            parsed = ingest.ingest_bytes("lines.pdf", data)
        self.assertEqual(parsed.plain_text, "public\npolicy")
        self.assertEqual(page.get_text.call_args.kwargs["flags"] & module.TEXT_PRESERVE_IMAGES, 0)
        document.close.assert_called_once()

    def test_pymupdf_compatibility_renderer_caps_dimensions_and_closes_document(self):
        pixmap = SimpleNamespace(width=4, height=6, tobytes=lambda kind: b"fitz-png")
        page = SimpleNamespace(rect=SimpleNamespace(width=2, height=3), get_pixmap=Mock(return_value=pixmap))
        class Document:
            is_encrypted, page_count = False, 1
            close = Mock()
            def __getitem__(self, index): return page
        document = Document()
        module = SimpleNamespace(open=lambda **kwargs: document, Matrix=lambda *values: values, csGRAY="gray")
        with patch.object(rendering, "renderer_backend", return_value=("pymupdf", module)):
            with rendering.open_pdf_renderer(b"test") as renderer:
                self.assertEqual(renderer.render_page(1), b"fitz-png")
        self.assertEqual(page.get_pixmap.call_args.kwargs["colorspace"], "gray")
        document.close.assert_called_once()

    def test_missing_renderer_is_an_explicit_blocking_quality_signal(self):
        adapter = SimpleNamespace(available=lambda: (True, "engine installed"))
        with patch.object(rendering, "renderer_backend", return_value=None):
            parsed = ingest.ingest_bytes("scan.pdf", pdf_fixture((None,)), ocr=adapter)
        self.assertFalse(parsed.quality.ocr_available)
        self.assertTrue(any(w.code == "pdf-renderer-unavailable" and w.severity == "critical" for w in parsed.warnings))

    def test_windows_default_tesseract_installation_is_found_without_path(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "Programs/Tesseract-OCR/tesseract.exe"
            path.parent.mkdir(parents=True)
            path.write_bytes(b"installation fixture")
            with patch.object(ingest.shutil, "which", return_value=None), patch.object(ingest.sys, "platform", "win32"), patch.dict(os.environ, {"LOCALAPPDATA": folder, "ProgramFiles": "", "ProgramFiles(x86)": ""}):
                self.assertEqual(Path(ingest._tesseract_executable()), path)

    def test_tesseract_subprocesses_are_hidden_and_tsv_preserves_lines_and_languages(self):
        phrases = ["English 简体 繁體", "Straße für français été", "日本語かな Русский язык", "lingua Latīna æ œ"]
        def run(command, **kwargs):
            if kwargs.get("creationflags") != 0x08000000:
                raise AssertionError("console process not hidden")
            if "--version" in command:
                return CompletedProcess(command, 0, "tesseract fixture\n", "")
            if "--list-langs" in command:
                return CompletedProcess(command, 0, "List of available languages:\neng\n", "")
            lines = ["level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"]
            for line, text in enumerate(phrases, 1):
                confidence = "NaN" if line == 4 else "99"
                lines.append(f"5\t1\t1\t1\t{line}\t1\t0\t0\t10\t10\t{confidence}\t{text}")
            Path(str(command[2]) + ".tsv").write_text("\n".join(lines), encoding="utf-8")
            return CompletedProcess(command, 0, "", "")
        with patch.object(ingest.sys, "platform", "win32"), patch.object(ingest.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), patch.object(ingest.subprocess, "run", side_effect=run):
            adapter = ingest.TesseractOCR("tesseract", language="eng")
            result = adapter.recognize_pdf_page(b"png", page_number=1, language="eng")
        self.assertEqual(result["text"], "\n".join(phrases))
        self.assertEqual(result["confidence"], 0)
        self.assertEqual(result["low_confidence_words"], 1)

    def test_doctor_does_not_offer_pymupdf_automatic_install_and_requires_renderer_for_ocr(self):
        original_import = builtins.__import__
        def unavailable_renderer(name, *args, **kwargs):
            if name in {"pypdfium2", "fitz"}:
                raise ImportError("unavailable")
            return original_import(name, *args, **kwargs)
        with patch("builtins.__import__", side_effect=unavailable_renderer), patch.object(ingest, "TesseractOCR", return_value=SimpleNamespace(available=lambda: (True, "installed"))):
            rows = {row["name"]: row for row in ingest.doctor_dependencies()}
        self.assertFalse(rows["pymupdf"]["repairable"])
        self.assertTrue(rows["tesseract"]["available"])
        self.assertFalse(rows["pdf-ocr"]["available"])
        self.assertIn("pypdfium2", rows["pdf-ocr"]["detail"])


if __name__ == "__main__":
    unittest.main()
