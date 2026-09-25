"""Markdown import/export must preserve cell content and literal examples."""
import unittest

from document_review_ingest import ingest_bytes
from document_review_model import model_to_markdown


def parse(text):
    return ingest_bytes('paper.md', text.encode('utf-8'))


class MarkdownFidelityTests(unittest.TestCase):
    def test_escaped_pipes_empty_edge_cells_and_separator_like_data_survive(self):
        text = '| | Label | |\n| --- | --- | --- |\n| | a\\|b | |\n| --- | --- | --- |'
        document = parse(text)
        table = next(b for b in document.blocks if b.kind == 'table')
        expected = [['', 'Label', ''], ['', 'a|b', ''], ['---', '---', '---']]
        self.assertEqual(table.attrs['rows'], expected)
        self.assertEqual(document.quality.table_count, 1)
        restored = parse(model_to_markdown(document))
        self.assertEqual(next(b for b in restored.blocks if b.kind == 'table').attrs['rows'], expected)
        data = next(b for b in document.blocks if b.kind == 'table_cell' and b.text == 'a|b')
        self.assertEqual((data.location.row, data.location.column, data.attrs['source_line']), (1, 1, 3))

    def test_invalid_delimiter_rows_do_not_swallow_prose(self):
        for text in ('a | b\n--- not a delimiter\ntext | evidence',
                     '| a | b |\n| --- |\n| one | two |',
                     '| a | b |\n| --- | prose |\n| one | two |'):
            with self.subTest(text=text):
                document = parse(text)
                self.assertFalse(any(b.kind == 'table' for b in document.blocks))
                self.assertIn(text.splitlines()[1], document.plain_text)

    def test_fenced_examples_keep_structure_markers_and_whitespace_literal(self):
        for opener, closer in (('```md', '```'), ('~~~~', '~~~~~'), ('````', '`````')):
            with self.subTest(opener=opener):
                literal = opener + '\r\n# Not a title\r\n  indented  \r\n\r\n| a | b |\r\n| --- | --- |\r\n' + closer
                text = literal + '\r\n# Real title\r\nAfter.'
                document = parse(text)
                self.assertEqual(document.title, 'Real title')
                self.assertFalse(any(b.kind == 'table' for b in document.blocks))
                block = next(b for b in document.blocks if b.attrs.get('fenced_code'))
                self.assertEqual(block.text, literal)
                self.assertEqual(text[block.location.char_start:block.location.char_end], literal)
                self.assertIn(literal, model_to_markdown(document))

    def test_unclosed_or_shorter_fences_do_not_promote_embedded_headings(self):
        text = '````\n```\n# Still code\n'
        document = parse(text)
        self.assertEqual(document.title, 'paper')
        self.assertEqual(len(document.blocks), 1)
        self.assertEqual(document.blocks[0].text, text.rstrip('\n'))

    def test_table_backslashes_and_pipes_round_trip_without_column_changes(self):
        text = r'| value |' + '\n| --- |\n' + r'| C:\folder\\end\|tail |'
        original = parse(text)
        rows = original.blocks[0].attrs['rows']
        restored = parse(model_to_markdown(original))
        self.assertEqual(restored.blocks[0].attrs['rows'], rows)
        self.assertEqual(rows[1], ['C:\\folder\\end|tail'])

    def test_ragged_rows_preserve_extra_evidence_and_warn(self):
        document = parse('| a | b |\n| --- | --- |\n| one | two | extra evidence |')
        self.assertEqual(document.blocks[0].attrs['rows'][1], ['one', 'two', 'extra evidence'])
        self.assertIn('markdown-ragged-table', {warning.code for warning in document.warnings})

    def test_block_starters_terminate_tables_and_are_not_table_headers(self):
        for start in ('# Heading | title', '> Quoted | text', '- List | item'):
            with self.subTest(start=start):
                document = parse(start + '\n| --- | --- |')
                self.assertFalse(any(block.kind == 'table' for block in document.blocks))
                document = parse('| a | b |\n| --- | --- |\n' + start)
                self.assertEqual(document.blocks[0].attrs['rows'], [['a', 'b']])
                self.assertNotEqual(document.blocks[-1].kind, 'table_cell')
