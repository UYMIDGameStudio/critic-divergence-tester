"""Source locations refer to decoded original characters, not normalized lines."""
import unittest

from document_review_ingest import ingest_bytes


class TextSourceLocationTests(unittest.TestCase):
    def test_character_ranges_preserve_original_line_endings_and_unicode(self):
        for newline in ('\n', '\r\n', '\r', '\u2028', '\u2029', '\f'):
            for encoding in ('utf-8', 'utf-16', 'utf-32'):
                with self.subTest(newline=repr(newline), encoding=encoding):
                    lines = ['# 論文 🧪', '', '  Français · Русский · Latīna  ', '- 日本語', '> Evidence']
                    text = newline.join(lines)
                    document = ingest_bytes('paper.md', text.encode(encoding))
                    self.assertEqual(document.metadata['character_offset_basis'], 'decoded-original-text')
                    self.assertEqual(document.metadata['character_offset_unit'], 'unicode-code-points')
                    for block in document.blocks:
                        line_number = block.attrs['source_line']
                        expected = lines[line_number - 1]
                        start = sum(len(line) + len(newline) for line in lines[:line_number - 1])
                        self.assertEqual(block.location.char_start, start)
                        self.assertEqual(text[block.location.char_start:block.location.char_end], expected)
                        mapping = next(item for item in document.source_to_block if item['block_id'] == block.block_id)
                        self.assertEqual((mapping['char_start'], mapping['char_end']), (start, start + len(expected)))

    def test_markdown_cells_map_to_actual_rows_after_separator(self):
        text = '# Title\r\n\r\n| A | B |\r\n| --- | --- |\r\n| One | Two |\r\n| Three | Four |\r\n\r\nAfter table'
        document = ingest_bytes('paper.md', text.encode())
        by_id = {block.block_id: block for block in document.blocks}
        expected = {'A': 3, 'B': 3, 'One': 5, 'Two': 5, 'Three': 6, 'Four': 6}
        for mapping in document.source_to_block:
            if mapping.get('kind') == 'table_cell':
                block = by_id[mapping['block_id']]
                self.assertEqual(mapping['source_line'], expected[block.text])
                self.assertEqual(block.attrs['source_line'], expected[block.text])
        after = next(block for block in document.blocks if block.text == 'After table')
        self.assertEqual(after.location.char_start, text.index('After table'))
        self.assertEqual(after.location.char_end, len(text))

    def test_mixed_line_endings_and_final_newline_preserve_ranges(self):
        text = 'First\r\n\nSecond\rThird\u2028Final\r\n'
        document = ingest_bytes('paper.txt', text.encode())
        for block in document.blocks:
            self.assertEqual(block.location.char_start, text.index(block.text))
            self.assertEqual(text[block.location.char_start:block.location.char_end], block.text)
