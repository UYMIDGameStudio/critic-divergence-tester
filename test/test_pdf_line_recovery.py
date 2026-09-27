"""Geometry-based PDF recovery: actual PDF plus rejection/fidelity controls."""
import hashlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import document_review_ingest as ingest
from studio_selftest import _fragmented_pdf_fixture, _pdf_fixture


class PDFLineRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.words = "These are some short words drawn on the same line but stored in separate text objects."
        self.plain = "\n \n".join(self.words.split())
        self.layout = "These are some short words drawn on the\n\nsame line but stored in separate text objects."

    def test_actual_transformed_pdf_reproduces_and_recovers_word_per_line(self):
        try:
            import pypdf
        except ImportError:
            self.skipTest("pypdf optional dependency is not installed")
        raw = _fragmented_pdf_fixture()
        plain = pypdf.PdfReader(io.BytesIO(raw)).pages[0].extract_text().strip()
        self.assertTrue(ingest._fragmented_pdf_lines(plain))
        with patch.object(ingest, "_pdf_backend", return_value=("pypdf", pypdf)):
            document = ingest.ingest_bytes("english.pdf", raw)
        self.assertEqual(document.plain_text.split(), self.words.split())
        self.assertLessEqual(len(document.plain_text.splitlines()), 5)
        self.assertEqual(document.source.sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(document.blocks[0].location.page, 1)
        self.assertEqual(document.source_to_block[0]["block_id"], document.blocks[0].block_id)
        self.assertEqual(document.metadata["layout_recovered_pages"], [1])
        self.assertFalse(document.metadata["fragmented_pages"])
        receipt = document.blocks[0].attrs["text_extraction"]
        self.assertEqual(receipt["plain_text_sha256"], hashlib.sha256(plain.encode()).hexdigest())
        self.assertEqual(receipt["layout_recovery"], "recovered")
        self.assertTrue(document.quality.requires_confirmation)

    def test_recovery_keeps_paragraph_gaps_and_indentation(self):
        candidate = self.layout.replace("\n\nsame", "\n\n    same")
        page = SimpleNamespace(extract_text=Mock(return_value=candidate))
        result, receipt = ingest._recover_pdf_layout(page, self.plain)
        self.assertEqual(result, candidate)
        self.assertEqual(receipt["mode"], "layout")
        page.extract_text.assert_called_once_with(extraction_mode="layout")

    def test_ordinary_prose_and_short_poem_do_not_request_layout(self):
        for text in (self.layout, "A\nsmall\npoem\nwith\nsix\nlines", "", "漢字かな\n原文", "這是正常的中文排版\n" * 20, "日本語の文章です\n" * 20):
            page = SimpleNamespace(extract_text=Mock())
            self.assertEqual(ingest._recover_pdf_layout(page, text), (text, {"mode": "plain"}))
            page.extract_text.assert_not_called()

    def test_real_vertical_poem_is_not_flattened(self):
        page = SimpleNamespace(extract_text=Mock(return_value=self.plain))
        result, receipt = ingest._recover_pdf_layout(page, self.plain)
        self.assertEqual(result, self.plain)
        self.assertEqual(receipt["reason"], "line-layout-not-improved")
        self.assertEqual(receipt["layout_recovery"], "not-needed")

    def test_missing_rotated_text_or_reordered_columns_rejected(self):
        for candidate in (self.layout.replace("short ", ""), " ".join(reversed(self.words.split()))):
            page = SimpleNamespace(extract_text=Mock(return_value=candidate))
            result, receipt = ingest._recover_pdf_layout(page, self.plain)
            self.assertEqual(result, self.plain)
            self.assertEqual(receipt["reason"], "text-sequence-changed")

    def test_word_number_sign_and_punctuation_changes_are_rejected(self):
        for before, after in (("the rapist", "therapist"), ("a dog", "ad og"),
                              ("1 000", "1000"), ("1 . 5", "1.5"),
                              ("a < = b", "a <= b"), ("evidence?", "evidence!")):
            with self.subTest(before=before):
                plain = self.plain + "\n" + "\n".join(before.split())
                page = SimpleNamespace(extract_text=Mock(return_value=self.layout + " " + after))
                self.assertEqual(ingest._recover_pdf_layout(page, plain)[0], plain)

    def test_detached_terminal_punctuation_in_reference_is_supported(self):
        plain = self.plain + "\nhttps://example.org/123\n."
        candidate = self.layout + "\nhttps://example.org/123."
        page = SimpleNamespace(extract_text=Mock(return_value=candidate))
        self.assertEqual(ingest._recover_pdf_layout(page, plain)[0], candidate)

    def test_layout_failure_retains_text_and_does_not_route_to_ocr(self):
        def extract(**kwargs):
            if kwargs:
                raise RuntimeError("unsupported layout")
            return self.plain
        library = SimpleNamespace(PdfReader=lambda *a, **k: SimpleNamespace(is_encrypted=False, pages=[SimpleNamespace(extract_text=extract)]))
        with patch.object(ingest, "_pdf_backend", return_value=("pypdf", library)):
            document = ingest.ingest_bytes("preserved.pdf", _pdf_fixture())
        self.assertEqual(document.plain_text, self.plain)
        self.assertEqual(document.metadata["fragmented_pages"], [1])
        self.assertTrue(document.quality.suspected_reading_order)
        codes = [warning.code for warning in document.warnings]
        self.assertIn("pdf-fragmented-lines", codes)
        self.assertNotIn("scan-pages-detected", codes)


if __name__ == "__main__":
    unittest.main()
