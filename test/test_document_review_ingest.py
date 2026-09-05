from __future__ import annotations

import io
import sys
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import document_review_ingest as ingest  # noqa: E402


class _UnderreportedInfo:
    filename = "word/document.xml"
    external_attr = 0
    flag_bits = 0
    file_size = 1
    compress_size = 1


class _UnderreportedArchive:
    def infolist(self):
        return [_UnderreportedInfo()]

    def open(self, info, mode):
        return io.BytesIO(b"x" * 32)

    def close(self):
        return None


class DocumentReviewIngestSafetyTests(unittest.TestCase):
    def test_utf16_dtd_is_rejected_before_entity_expansion(self) -> None:
        text = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE doc [<!ENTITY example "expanded">]><doc>&example;</doc>'
        archive = ingest._VerifiedZipArchive({'word/document.xml': text.encode('utf-16')})
        with self.assertRaisesRegex(ingest.IngestionError, '实体声明|DTD'):
            ingest._docx_xml(archive, 'word/document.xml')

    def test_plain_utf16_xml_remains_supported(self) -> None:
        archive = ingest._VerifiedZipArchive({'word/document.xml': '<doc>正文</doc>'.encode('utf-16')})
        self.assertEqual(ingest._docx_xml(archive, 'word/document.xml').text, '正文')

    def test_archive_is_closed_when_entry_count_is_rejected(self) -> None:
        archive = _UnderreportedArchive()
        with patch.object(archive, 'close') as close, patch.object(ingest.zipfile, 'ZipFile', return_value=archive):
            with self.assertRaisesRegex(ingest.IngestionError, '条目过多'):
                ingest._zip_safety(b'fake', ingest.IngestionLimits(max_docx_entries=0))
            close.assert_called_once()

    def test_corrupt_deflate_is_a_reportable_ingestion_error(self) -> None:
        archive = _UnderreportedArchive()
        with patch.object(archive, 'open', side_effect=zlib.error('corrupt stream')):
            with patch.object(ingest.zipfile, 'ZipFile', return_value=archive):
                with self.assertRaisesRegex(ingest.IngestionError, '解压失败'):
                    ingest._zip_safety(b'fake', ingest.IngestionLimits())

    def test_docx_limit_uses_streamed_bytes_not_declared_file_size(self) -> None:
        limits = ingest.IngestionLimits(
            max_docx_uncompressed_bytes=8,
            max_docx_compression_ratio=1000,
        )
        with patch.object(ingest.zipfile, "ZipFile", return_value=_UnderreportedArchive()):
            with self.assertRaisesRegex(ingest.IngestionError, "解压后体积过大"):
                ingest._zip_safety(b"fake zip bytes", limits)


if __name__ == "__main__":
    unittest.main()
