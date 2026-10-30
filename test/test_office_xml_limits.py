"""Bound XML while building it, before IR and adapter checks can run."""
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import document_review_ingest as ingest
from document_review_studio import DocumentReviewProject
from test.test_docx_review_visibility import package as docx_package
from test.test_office_formats import zipped, xlsx_entries, pptx_entries, odt_entries


class OfficeXMLLimitsTests(unittest.TestCase):
    def test_element_limit_refuses_before_allocating_the_next_element(self):
        allocated = []
        builder = ingest._SafeDocxTreeBuilder

        class ObservedBuilder(builder):
            def start(self, tag, attrs):
                element = super().start(tag, attrs)
                allocated.append(element)
                return element

        raw = b'<root>' + b'<node/>' * 3000 + b'</root>'
        archive = ingest._VerifiedZipArchive({'part.xml': raw}, replace(ingest.IngestionLimits(), max_xml_elements=20))
        with patch.object(ingest, '_SafeDocxTreeBuilder', ObservedBuilder):
            with self.assertRaisesRegex(ingest.IngestionError, '单个部件'):
                ingest._docx_xml(archive, 'part.xml')
        self.assertEqual(len(allocated), 20)
        self.assertTrue(all(isinstance(node.tag, str) for node in allocated))

    def test_exact_element_and_depth_boundaries_and_sibling_depth(self):
        raw = b'<root><one/><two/><three/></root>'
        limits = replace(ingest.IngestionLimits(), max_xml_elements=4, max_xml_depth=1)
        root = ingest._docx_xml(ingest._VerifiedZipArchive({'part.xml': raw}, limits), 'part.xml')
        self.assertEqual([child.tag for child in root], ['one', 'two', 'three'])
        for changes in ({'max_xml_elements': 3}, {'max_xml_depth': 0}):
            with self.subTest(changes=changes), self.assertRaises(ingest.IngestionError):
                ingest._docx_xml(ingest._VerifiedZipArchive({'part.xml': raw}, replace(limits, **changes)), 'part.xml')
        root_only = ingest._VerifiedZipArchive({'part.xml': b'<root/>'}, replace(limits, max_xml_depth=0))
        self.assertEqual(ingest._docx_xml(root_only, 'part.xml').tag, 'root')
        deep = ingest._VerifiedZipArchive({'part.xml': b'<n>' * 300 + b'</n>' * 300})
        with self.assertRaisesRegex(ingest.IngestionError, '嵌套深度'):
            ingest._docx_xml(deep, 'part.xml')

    def test_invalid_numeric_limits_cannot_silently_disable_the_guards(self):
        for field in ('max_xml_elements', 'max_xml_total_elements', 'max_xml_depth'):
            for value in (True, -1, 0.5, float('nan'), float('inf'), None, '100'):
                with self.subTest(field=field, value=value), self.assertRaisesRegex(ingest.IngestionError, '非负整数'):
                    archive = ingest._VerifiedZipArchive({'part.xml': b'<root/>'}, replace(ingest.IngestionLimits(), **{field: value}))
                    ingest._docx_xml(archive, 'part.xml')
        for field in ('max_xml_elements', 'max_xml_total_elements'):
            with self.subTest(field=field), self.assertRaises(ingest.IngestionError):
                archive = ingest._VerifiedZipArchive({'part.xml': b'<root/>'}, replace(ingest.IngestionLimits(), **{field: 0}))
                ingest._docx_xml(archive, 'part.xml')

    def test_aggregate_budget_counts_parts_and_repeated_reads_but_not_missing_parts(self):
        limits = replace(ingest.IngestionLimits(), max_xml_elements=3, max_xml_total_elements=4)
        entries = {'first.xml': b'<root><one/></root>', 'second.xml': b'<root><two/></root>'}
        for second in ('first.xml', 'second.xml'):
            with self.subTest(second=second):
                archive = ingest._VerifiedZipArchive(entries, limits)
                self.assertIsNone(ingest._docx_xml(archive, 'missing.xml'))
                ingest._docx_xml(archive, 'first.xml')
                ingest._docx_xml(archive, second)
                with self.assertRaisesRegex(ingest.IngestionError, '总解析'):
                    ingest._docx_xml(archive, 'first.xml')
                # A new ingestion owns a fresh budget; no persistent cache.
                fresh = ingest._VerifiedZipArchive(entries, limits)
                self.assertEqual(ingest._docx_xml(fresh, 'second.xml').tag, 'root')

    def test_failed_parse_does_not_reset_or_replenish_the_archive_budget(self):
        archive = ingest._VerifiedZipArchive({'broken.xml': b'<root><one>', 'next.xml': b'<root/>'},
            replace(ingest.IngestionLimits(), max_xml_total_elements=2))
        with self.assertRaisesRegex(ingest.IngestionError, '损坏'):
            ingest._docx_xml(archive, 'broken.xml')
        with self.assertRaisesRegex(ingest.IngestionError, '总解析'):
            ingest._docx_xml(archive, 'next.xml')

    def test_limits_apply_to_real_word_spreadsheet_presentation_and_odt_packages(self):
        cases = [('docx', docx_package()), ('xlsx', zipped(xlsx_entries())),
                 ('pptx', zipped(pptx_entries())), ('odt', zipped(odt_entries()))]
        for extension, raw in cases:
            baseline = ingest.ingest_bytes('paper.' + extension, raw)
            self.assertTrue(baseline.blocks)
            for changes in ({'max_xml_elements': 1}, {'max_xml_depth': 0}, {'max_xml_total_elements': 1}):
                with self.subTest(extension=extension, changes=changes), self.assertRaises(ingest.IngestionError):
                    ingest.ingest_bytes('paper.' + extension, raw, limits=replace(ingest.IngestionLimits(), **changes))
            self.assertEqual(ingest.ingest_bytes('paper.' + extension, raw).to_dict(), baseline.to_dict())

    def test_legacy_duck_typed_limits_receive_safe_xml_defaults(self):
        fields = {key: value for key, value in asdict(ingest.IngestionLimits()).items() if not key.startswith('max_xml_')}
        document = ingest.ingest_bytes('paper.xlsx', zipped(xlsx_entries()), limits=SimpleNamespace(**fields))
        self.assertTrue(document.blocks)

    def test_aggregate_limit_spans_multiple_real_package_parts(self):
        builder = ingest._SafeDocxTreeBuilder
        for extension, raw in (('xlsx', zipped(xlsx_entries())), ('pptx', zipped(pptx_entries()))):
            counts = []

            class CountParts(builder):
                def end(self, tag):
                    result = super().end(tag)
                    if self.depth == -1:
                        counts.append(self.elements)
                    return result

            with patch.object(ingest, '_SafeDocxTreeBuilder', CountParts):
                ingest.ingest_bytes('paper.' + extension, raw)
            self.assertGreater(len(counts), 1)
            self.assertLess(max(counts), sum(counts))
            limits = replace(ingest.IngestionLimits(), max_xml_total_elements=sum(counts) - 1)
            with self.subTest(extension=extension), self.assertRaisesRegex(ingest.IngestionError, '总解析'):
                ingest.ingest_bytes('paper.' + extension, raw, limits=limits)

    def test_xml_failure_preserves_original_project_and_never_emits_partial_ir(self):
        body = '<w:p>' + '<w:r/>' * 100 + '<w:r><w:t>Bounded text.</w:t></w:r></w:p>'
        raw = docx_package(body)
        with tempfile.TemporaryDirectory() as temp:
            project = DocumentReviewProject.create(temp, filename='paper.docx', content=raw,
                limits=replace(ingest.IngestionLimits(), max_xml_elements=12))
            self.assertEqual(project.state()['extraction_state'], 'blocked')
            self.assertFalse(project.document_path.exists())
            self.assertEqual((project.root / 'source/paper.docx').read_bytes(), raw)
            self.assertFalse(project.can_review()[0])
            fresh = DocumentReviewProject.create(temp, filename='paper.docx', content=raw, new_project=True)
            self.assertEqual(fresh.document().plain_text, 'Bounded text.')
            self.assertEqual(project.state()['extraction_state'], 'blocked')
            self.assertEqual(fresh.integrity_errors(), [])
