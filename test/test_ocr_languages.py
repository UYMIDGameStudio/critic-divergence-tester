"""Selected OCR language checks using a controlled subprocess boundary."""
from pathlib import Path
from subprocess import CompletedProcess
import unittest
from unittest.mock import patch

import document_review_ingest as ingest
from document_review_model import ExtractionWarning


def tesseract_process(installed, *, text="日本語の文章", list_returncode=0):
    def run(command, **kwargs):
        if "--version" in command:
            return CompletedProcess(command, 0, "tesseract 5.5.0\n", "")
        if "--list-langs" in command:
            return CompletedProcess(command, list_returncode, "List of available languages (test):\n" + "\n".join(installed), "")
        output = Path(str(command[2]) + ".tsv")
        output.write_text("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
                          + "\t".join(["5", "1", "1", "1", "1", "1", "0", "0", "10", "10", "98.5", text]) + "\n", encoding="utf-8")
        return CompletedProcess(command, 0, "", "")
    return run


class OCRLanguageTests(unittest.TestCase):
    def test_each_supported_language_only_requires_its_selected_package(self):
        for language in ("eng", "chi_sim", "chi_tra", "deu", "fra", "jpn", "rus", "lat"):
            with self.subTest(language=language), patch.object(ingest.subprocess, "run", side_effect=tesseract_process({language})):
                adapter = ingest.TesseractOCR("tesseract", language=language)
                self.assertTrue(adapter.available()[0])

    def test_default_retains_three_original_packages(self):
        with patch.object(ingest.subprocess, "run", side_effect=tesseract_process({"eng"})):
            adapter = ingest.TesseractOCR("tesseract")
            available, message = adapter.available()
        self.assertFalse(available)
        self.assertIn("chi_sim", message)
        self.assertIn("chi_tra", message)

    def test_missing_packages_are_reported_for_the_requested_combination(self):
        with patch.object(ingest.subprocess, "run", side_effect=tesseract_process({"eng", "deu"})):
            adapter = ingest.TesseractOCR("tesseract", language="deu+fra")
            available, message = adapter.available()
        self.assertFalse(available)
        self.assertIn("fra", message)
        self.assertNotIn("chi_sim", message)
        self.assertNotIn("chi_tra", message)

    def test_recognition_override_checks_the_actual_language_and_preserves_text(self):
        with patch.object(ingest.subprocess, "run", side_effect=tesseract_process({"eng", "jpn"})) as process:
            adapter = ingest.TesseractOCR("tesseract")
            result = adapter.recognize_pdf_page(b"test page bytes", page_number=1, language="jpn+eng")
        command = process.call_args_list[-1].args[0]
        self.assertEqual(command[command.index("-l") + 1], "jpn+eng")
        self.assertEqual(result["language"], "jpn+eng")
        self.assertEqual(result["text"], "日本語の文章")

    def test_invalid_combinations_fail_before_starting_a_subprocess(self):
        for language in ("", "eng++fra", "eng+eng", "eng+../rus", "eng --psm 0", "unknown", None):
            with self.subTest(language=language), patch.object(ingest.subprocess, "run") as process:
                with self.assertRaises(ingest.IngestionError):
                    ingest.TesseractOCR("tesseract", language=language)
                process.assert_not_called()
        with patch.object(ingest.subprocess, "run", side_effect=tesseract_process({"eng"})) as process:
            adapter = ingest.TesseractOCR("tesseract")
            process.reset_mock()
            with self.assertRaises(ingest.IngestionError):
                adapter.recognize_pdf_page(b"page", page_number=1, language="eng+../../outside")
            process.assert_not_called()

    def test_failed_language_inventory_cannot_be_reported_available(self):
        with patch.object(ingest.subprocess, "run", side_effect=tesseract_process({"eng"}, list_returncode=2)):
            adapter = ingest.TesseractOCR("tesseract", language="eng")
            available, message = adapter.available()
        self.assertFalse(available)
        self.assertIn("状态", message)

    def test_pdf_ingestion_constructs_adapter_with_requested_language(self):
        document = ingest.ingest_bytes("source.txt", b"A page")
        document.warnings.append(ExtractionWarning("scan-pages-detected", "high", "test scan", details={"pages": [1]}))
        with patch.object(ingest, "_pdf_text", return_value=document), patch.object(ingest, "TesseractOCR") as adapter:
            adapter.return_value.available.return_value = (False, "missing fra")
            parsed = ingest.ingest_bytes("scan.pdf", b"test PDF", ocr_language="fra")
        adapter.assert_called_once_with(timeout_seconds=45, language="fra")
        self.assertTrue(any(warning.code == "ocr-unavailable" and "fra" in warning.message for warning in parsed.warnings))

    def test_language_normalization_has_no_subprocess_dependency(self):
        with patch.object(ingest.subprocess, "run") as process:
            self.assertEqual(ingest.TesseractOCR.normalize_language(" ENG + fra "), "eng+fra")
            process.assert_not_called()

    def test_corrupt_ocr_output_is_rejected_without_replacement_characters(self):
        ordinary = tesseract_process({"eng"})

        def corrupt_output(command, **kwargs):
            result = ordinary(command, **kwargs)
            if "--version" not in command and "--list-langs" not in command:
                Path(str(command[2]) + ".tsv").write_bytes(b"invalid UTF-8: \xff")
            return result

        with patch.object(ingest.subprocess, "run", side_effect=corrupt_output):
            adapter = ingest.TesseractOCR("tesseract", language="eng")
            with self.assertRaisesRegex(ingest.IngestionError, "UTF-8"):
                adapter.recognize_pdf_page(b"page", page_number=1, language="eng")


if __name__ == "__main__":
    unittest.main()
