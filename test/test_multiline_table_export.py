"""Normalized Markdown must not split one multiline source cell into rows."""
import csv
import io
import unittest

from document_review_ingest import ingest_bytes
from document_review_model import model_to_markdown


class MultilineTableExportTests(unittest.TestCase):
    def test_csv_multiline_and_literal_markup_round_trip_without_new_blocks(self):
        rows = [['name', 'evidence'], ['論文', 'first\r\n# not a heading\nlast'],
                ['literal', '<br> & &#10; `ticks` \\path | a'],
                ['adjacent', '\\<br> \\&amp; `a<br>b`'], ['blank', '\n\n'],
                ['spaces', '  preserve\t '], ['only whitespace', '\t  ']]
        stream = io.StringIO(newline='')
        csv.writer(stream).writerows(rows)
        original = ingest_bytes('paper.csv', stream.getvalue().encode())
        markdown = model_to_markdown(original)
        restored = ingest_bytes('paper.md', markdown.encode())
        self.assertEqual(restored.blocks[0].attrs['rows'], rows)
        self.assertFalse(any(block.kind == 'heading' for block in restored.blocks))
        self.assertEqual(sum(block.kind == 'table' for block in restored.blocks), 1)
        self.assertNotIn('<br> & &#10;', markdown)

    def test_markdown_breaks_and_entities_decode_outside_literal_code(self):
        document = ingest_bytes('paper.md', b'| a | b |\n| --- | --- |\n| first<br />second | &lt;br&gt; &amp; |\n| `a<br>b &amp;` | \\<br> |')
        self.assertEqual(document.blocks[0].attrs['rows'],
                         [['a', 'b'], ['first\nsecond', '<br> &'], ['`a<br>b &amp;`', '<br>']])

    def test_export_html_like_source_cannot_become_an_active_table_tag(self):
        document = ingest_bytes('paper.csv', b'name,value\nx,<img src=x onerror=alert(1)>')
        markdown = model_to_markdown(document)
        self.assertNotIn('<img', markdown)
        self.assertIn('&lt;img', markdown)
        restored = ingest_bytes('paper.md', markdown.encode())
        self.assertEqual(restored.blocks[0].attrs['rows'][1][1], '<img src=x onerror=alert(1)>')

    def test_table_formula_backslashes_remain_literal(self):
        formula = r'$S=\{x\mid x\_1 > 0\}$'
        document = ingest_bytes('paper.md', ('| formula |\n| --- |\n| ' + formula + ' |').encode())
        self.assertEqual(document.blocks[0].attrs['rows'][1], [formula])
        restored = ingest_bytes('paper.md', model_to_markdown(document).encode())
        self.assertEqual(restored.blocks[0].attrs['rows'][1], [formula])
