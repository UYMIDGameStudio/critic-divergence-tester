"""HTML numbering is semantic data, including explicit resets and nested ownership."""
import io
import json
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree as ET

from document_review_ingest import IngestionError, ingest_bytes
from document_review_model import ReviewContext, model_to_markdown
from document_review_studio import DocumentReviewProject, _minimal_docx


def parse(source):
    return ingest_bytes('paper.html', source.encode())


def markers(document):
    return [b.attrs['list_marker'] for b in document.blocks if b.attrs.get('list_item_start')]


class HTMLListFidelityTests(unittest.TestCase):
    def test_start_reset_signed_values_and_invalid_attribute_defaults(self):
        for source, expected in (
            ('<ol start="3"><li>A<li value="7">B<li>C</ol>', ['3.', '7.', '8.']),
            ('<ol start="-2"><li>A<li>B<li value="+0">C<li>D</ol>', ['-2.', '-1.', '0.', '1.']),
            ('<ol start="invalid"><li value="bad">A<li>B</ol>', ['1.', '2.']),
            ('<ol start=" +12tail"><li>A<li>B</ol>', ['12.', '13.']),
            ('<ol reversed start="5"><li>A<li value="2">B<li>C</ol>', ['5.', '2.', '1.']),
        ):
            with self.subTest(source=source):
                self.assertEqual(markers(parse(source)), expected)

    def test_reversed_default_counts_owned_items_including_empty_ones(self):
        document = parse('<ol reversed><li>A<ol start="8"><li>X<li>Y</ol>Tail<li></li><li>B</ol>')
        self.assertEqual(markers(document), ['3.', '8.', '9.', '1.'])
        blocks = {b.text: b for b in document.blocks}
        self.assertEqual(blocks['A'].attrs['list_item_id'], blocks['Tail'].attrs['list_item_id'])
        self.assertEqual(blocks['X'].attrs['parent_list_item_id'], blocks['A'].attrs['list_item_id'])
        self.assertEqual(blocks['X'].attrs['list_depth'], 1)
        self.assertNotEqual(blocks['X'].attrs['list_id'], blocks['A'].attrs['list_id'])
        self.assertTrue(blocks['Tail'].attrs['list_continuation'])
        self.assertNotEqual(blocks['Tail'].kind, 'list_item')

    def test_alpha_roman_and_css_range_fallbacks(self):
        cases = [('a', 26, ['z.', 'aa.']), ('A', 26, ['Z.', 'AA.']),
                 ('i', 9, ['ix.', 'x.']), ('I', 49, ['XLIX.', 'L.']),
                 ('I', 3999, ['MMMCMXCIX.', '4000.']), ('a', 0, ['0.', 'a.']),
                 ('unrecognized', 3, ['3.', '4.'])]
        for style, start, expected in cases:
            with self.subTest(style=style, start=start):
                self.assertEqual(markers(parse(f'<ol type="{style}" start="{start}"><li>A<li>B</ol>')), expected)
        self.assertEqual(markers(parse('<ol type="A"><li>A<li type="i">B<li>C</ol>')), ['A.', 'ii.', 'C.'])

    def test_multiblock_items_do_not_create_extra_numbers(self):
        document = parse('<ol start="7"><li><p>Premise</p><p>Explanation</p></li>'
                         '<li><h2>Conclusion</h2><p>Details</p></li></ol>')
        self.assertEqual(markers(document), ['7.', '8.'])
        self.assertEqual(model_to_markdown(document), '7. Premise\n\nExplanation\n\n## 8. Conclusion\n\nDetails\n')
        self.assertEqual(document.blocks[2].kind, 'heading')
        self.assertEqual(document.to_dict(), parse('<ol start="7"><li><p>Premise</p><p>Explanation</p></li>'
                         '<li><h2>Conclusion</h2><p>Details</p></li></ol>').to_dict())

    def test_exported_word_and_markdown_keep_visible_labels(self):
        document = parse('<ol type="I" start="4"><li>前提<li value="9">Premise<li>結論</ol>')
        expected = ['IV. 前提', 'IX. Premise', 'X. 結論']
        markdown = model_to_markdown(document)
        self.assertEqual(markdown.splitlines()[::2], expected)
        for data in (_minimal_docx(markdown), _minimal_docx(markdown, document=document)):
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                root = ET.fromstring(archive.read('word/document.xml'))
            ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
            actual = [''.join(t.text or '' for t in p.findall('.//w:t', ns)) for p in root.findall('./w:body/w:p', ns)]
            self.assertEqual(actual, expected)

    def test_hidden_lists_do_not_change_visible_numbering_or_leak_text(self):
        document = parse('<ol reversed><li>A<li hidden value="40">Secret<li>B</ol>'
                         '<div hidden><ol><li>Hidden</ol></div><ol><li>C<li>D</ol>')
        self.assertEqual(markers(document), ['2.', '1.', '1.', '2.'])
        self.assertNotIn('Secret', document.plain_text)
        self.assertNotIn('Hidden', document.plain_text)
        for hidden in ('hidden', 'style="display:none"'):
            document = parse(f'<ol><li>A<li {hidden}>Secret</ol><p>After</p>')
            self.assertEqual(document.plain_text, 'A\n\nAfter')
        for hidden in ('aria-hidden="true"', 'style="visibility:hidden"'):
            document = parse(f'<ol reversed><li>A<li {hidden}>Secret<li>B</ol>')
            self.assertEqual(markers(document), ['3.', '1.'])
            self.assertNotIn('Secret', document.plain_text)

    def test_optional_item_closing_cannot_escape_hidden_templates(self):
        for source in ('<ol><li>A<template><li>Secret</ol>Secret tail</template></li></ol><p>After</p>',
                       '<ol><li hidden>A<template><li>Secret</ol>Secret tail</template></li></ol><p>After</p>'):
            with self.subTest(source=source):
                document = parse(source)
                self.assertNotIn('Secret', document.plain_text)
                self.assertIn('After', document.plain_text)

    def test_large_number_attributes_fail_before_big_integer_conversion(self):
        for source in ('<ol start="' + '9' * 65 + '"><li>A</ol>',
                       '<ol><li value="' + '9' * 65 + '">A</ol>'):
            with self.subTest(source=source), self.assertRaises(IngestionError):
                parse(source)
        self.assertEqual(markers(parse('<ol start="' + '9' * 64 + '"><li>A</ol>')), ['9' * 64 + '.'])

    def test_lists_inside_tables_are_explicitly_marked_as_linearized(self):
        document = parse('<table><tr><td><ol start="7"><li>A<li>B</ol></td></tr></table>')
        self.assertIn('html-table-list-linearized', {w.code for w in document.warnings})
        self.assertIn('A', document.plain_text)
        self.assertIn('B', document.plain_text)

    def test_persisted_review_input_preserves_numbering_and_parent_relationship(self):
        with tempfile.TemporaryDirectory() as temp:
            project = DocumentReviewProject.create(temp, filename='paper.html',
                content=b'<ol start="7"><li>Premise<ul><li>Evidence</ul>Explanation<li>Conclusion</ol>')
            project.confirm_extraction('confirm')
            project.confirm_context(ReviewContext(document_type='memo', jurisdiction='unspecified',
                effective_date='2026-09-25', publisher_type='author', audience='reviewers').to_dict())
            payload = project.prompt('expression_ambiguity').split('## Internal document blocks\n```json\n', 1)[1].split('\n```', 1)[0]
            blocks = {b['text']: b for b in json.loads(payload)}
            self.assertEqual(blocks['Premise']['attrs']['list_marker'], '7.')
            self.assertEqual(blocks['Conclusion']['attrs']['list_marker'], '8.')
            self.assertEqual(blocks['Evidence']['attrs']['parent_list_item_id'], blocks['Premise']['attrs']['list_item_id'])
            self.assertEqual(blocks['Explanation']['attrs']['list_item_id'], blocks['Premise']['attrs']['list_item_id'])
            self.assertEqual(project.integrity_errors(), [])
