import base64
from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from document_review_ingest import ingest_bytes, ParserUnavailable
from document_review_studio import DocumentReviewProject
from document_review_ui import StudioApp
from test.test_document_review_studio import _docx


SAMPLES = {
    "en": ("English: author’s review.", "cp1252"),
    "zh_hans": ("简体中文：文档解析与审查。", "gb18030"),
    "zh_hant": ("繁體中文：文件解析與審查。", "big5"),
    "de": ("Deutsch: Grüße, äußerst große Straße.", "cp1252"),
    "fr": ("Français : élève, cœur, où, déjà.", "cp1252"),
    "ja": ("日本語：文章の解析と確認です。", "cp932"),
    "ru": ("Русский: проверка и разбор документа.", "cp1251"),
    "la": ("Līngua Latīna: æquus, cœlum, virtūs.", "utf-16"),
}


class MultilingualImportTests(unittest.TestCase):
    def test_eight_languages_keep_letters_and_original_bytes_through_project_ui(self):
        with tempfile.TemporaryDirectory() as temp:
            app = StudioApp.create(temp)
            for name, (text, encoding) in SAMPLES.items():
                with self.subTest(language=name):
                    raw = text.encode(encoding)
                    current = app.upload({"filename": name + ".txt", "content_base64": base64.b64encode(raw).decode(), "encoding": encoding})
                    project = current.project
                    self.assertEqual(project.document().plain_text, text)
                    self.assertEqual((project.root / "source" / (name + ".txt")).read_bytes(), raw)
                    self.assertEqual(project.document().source.sha256, hashlib.sha256(raw).hexdigest())
                    self.assertTrue(current.view()["selected"]["extraction"]["metadata"]["encoding"])
                    self.assertFalse(project.integrity_errors())

    def test_mixed_eight_languages_unicode_is_not_replaced_or_transliterated(self):
        text = "\n\n".join(value[0] for value in SAMPLES.values())
        for encoding in ("utf-8", "utf-16", "utf-32"):
            with self.subTest(encoding=encoding):
                document = ingest_bytes("mixed.md", text.encode(encoding))
                self.assertEqual(document.plain_text, text)

    def test_switching_encoding_before_confirmation_preserves_integrity(self):
        with tempfile.TemporaryDirectory() as temp:
            text, encoding = SAMPLES["zh_hant"]
            project = DocumentReviewProject.create(temp, filename="traditional.txt", content=text.encode(encoding))
            raw = (project.root / "source/traditional.txt").read_bytes()
            project.retry_extraction(encoding="big5")
            self.assertEqual(project.document().plain_text, text)
            self.assertFalse(project.integrity_errors())
            self.assertEqual((project.root / "source/traditional.txt").read_bytes(), raw)
            project.confirm_extraction("confirm")
            self.assertFalse(project.integrity_errors())

    def test_explicit_different_codec_creates_separate_project(self):
        with tempfile.TemporaryDirectory() as temp:
            raw = "中文內容".encode("big5")
            auto = DocumentReviewProject.create(temp, filename="text.txt", content=raw)
            specified = DocumentReviewProject.create(temp, filename="text.txt", content=raw, encoding="big5")
            self.assertNotEqual(auto.root, specified.root)
            self.assertEqual(specified.document().plain_text, "中文內容")

    def test_legacy_docx_content_keeps_original_binding(self):
        document = ingest_bytes("original.wps", _docx())
        self.assertEqual(document.source.extension, ".wps")
        self.assertEqual(document.source.sha256, hashlib.sha256(_docx()).hexdigest())
        self.assertTrue(document.blocks)

    def test_missing_legacy_converter_is_explicit_and_original_is_retained(self):
        with tempfile.TemporaryDirectory() as temp, patch("document_review_legacy.find_libreoffice", return_value=None):
            raw = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1binary-office-sample"
            with self.assertRaisesRegex(ParserUnavailable, "LibreOffice"):
                ingest_bytes("legacy.doc", raw)
            project = DocumentReviewProject.create(temp, filename="legacy.doc", content=raw)
            self.assertEqual(project.state()["extraction_state"], "blocked")
            self.assertEqual((project.root / "source/legacy.doc").read_bytes(), raw)
