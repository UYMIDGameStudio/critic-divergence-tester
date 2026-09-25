"""Bound text structure growth without truncating accepted manuscripts."""
from dataclasses import replace
import tempfile
import unittest
from unittest.mock import patch

from document_review_ingest import IngestionError, IngestionLimits, ingest_bytes
from document_review_studio import DocumentReviewProject


class TextCapacityTests(unittest.TestCase):
    def test_text_blocks_allow_boundary_and_reject_overflow_before_constructing_it(self):
        limits = replace(IngestionLimits(), max_text_blocks=3)
        self.assertEqual(len(ingest_bytes('paper.txt', b'one\ntwo\nthree', limits=limits).blocks), 3)
        from document_review_model import DocumentBlock
        with patch('document_review_ingest.DocumentBlock', wraps=DocumentBlock) as construct:
            with self.assertRaisesRegex(IngestionError, '文本块'):
                ingest_bytes('paper.txt', b'one\ntwo\nthree\nfour', limits=limits)
            self.assertEqual(construct.call_count, 3)

    def test_line_limit_counts_blank_and_fenced_lines_and_real_terminators(self):
        limits = replace(IngestionLimits(), max_text_lines=3)
        for text in ('one\r\ntwo\r\nthree', 'one\ntwo\nthree\n', '```\ncode\n```'):
            with self.subTest(text=text):
                ingest_bytes('paper.md', text.encode(), limits=limits)
        for text in ('one\r\ntwo\rthree\u2028four', '\n' * 4, '```\n\ncode\n```'):
            with self.subTest(text=text), self.assertRaisesRegex(IngestionError, '行数'):
                ingest_bytes('paper.md', text.encode(), limits=limits)

    def test_markdown_tables_apply_row_column_cell_and_block_budgets(self):
        raw = b'| a | b |\n| --- | --- |\n| one | two |'
        for fields in ({'max_office_rows': 1}, {'max_office_columns': 1},
                       {'max_office_cells': 3}, {'max_text_blocks': 4}):
            with self.subTest(fields=fields), self.assertRaises(IngestionError):
                ingest_bytes('paper.md', raw, limits=replace(IngestionLimits(), **fields))
        limits = replace(IngestionLimits(), max_office_rows=2, max_office_columns=2,
                         max_office_cells=4, max_text_blocks=5)
        document = ingest_bytes('paper.md', raw, limits=limits)
        self.assertEqual(document.blocks[0].attrs['rows'], [['a', 'b'], ['one', 'two']])
        with self.assertRaises(IngestionError):
            ingest_bytes('paper.md', raw + b'\n\n' + raw, limits=replace(limits, max_text_blocks=20))

    def test_non_table_pipe_prose_and_fenced_pipes_are_not_counted_as_cells(self):
        limits = replace(IngestionLimits(), max_office_columns=1, max_office_cells=1)
        for raw in (b'a | b | c\nOrdinary prose', b'```\na | b | c\n```'):
            with self.subTest(raw=raw):
                document = ingest_bytes('paper.md', raw, limits=limits)
                self.assertFalse(any(block.kind == 'table' for block in document.blocks))

    def test_rejected_project_keeps_raw_manuscript_and_no_partial_ir(self):
        raw = b'one\ntwo\nthree'
        with tempfile.TemporaryDirectory() as temp:
            project = DocumentReviewProject.create(temp, filename='paper.txt', content=raw,
                limits=replace(IngestionLimits(), max_text_blocks=2))
            self.assertEqual(project.state()['extraction_state'], 'blocked')
            self.assertEqual((project.root / 'source/paper.txt').read_bytes(), raw)
            self.assertFalse(project.document_path.exists())
            self.assertEqual(project.integrity_errors(), [])
