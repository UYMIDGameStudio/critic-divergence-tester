"""Actual OOXML output retains structured table content without source layout claims."""
import io
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree as ET

from document_review_ingest import ingest_bytes
from document_review_model import model_to_markdown
from document_review_studio import DocumentReviewProject, _minimal_docx
from test import test_document_review_studio as studio_fixture


W = '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'


def xml(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return ET.fromstring(archive.read('word/document.xml'))


def text(element):
    return ''.join(node.text or '' if node.tag == W + 't' else '\n' if node.tag in {W + 'br', W + 'cr'}
                   else '\t' if node.tag == W + 'tab' else '' for node in element.iter())


class NormalizedDocxTests(unittest.TestCase):
    def test_markdown_to_word_generates_real_tables_and_breaks(self):
        markdown = '# Title\n\n| name | evidence |\n| --- | --- |\n| A | first<br>second &lt;br&gt; &amp; |'
        root = xml(_minimal_docx(markdown))
        tables = root.findall('.//' + W + 'tbl')
        self.assertEqual(len(tables), 1)
        self.assertEqual([[text(cell) for cell in row.findall(W + 'tc')] for row in tables[0].findall(W + 'tr')],
                         [['name', 'evidence'], ['A', 'first\nsecond <br> &']])
        self.assertEqual(len(tables[0].findall('.//' + W + 'br')), 1)

    def test_structured_word_copy_preserves_tabs_spaces_and_literal_markup(self):
        document = ingest_bytes('paper.csv', b'name,evidence\r\nA,"  first\t\r\n# literal <br> | last  "')
        root = xml(_minimal_docx(model_to_markdown(document), document=document))
        cell = root.findall('.//' + W + 'tr')[1].findall(W + 'tc')[1]
        self.assertEqual(text(cell), '  first\t\n# literal <br> | last  ')
        self.assertEqual(len(cell.findall('.//' + W + 'tab')), 1)
        self.assertEqual(len(cell.findall('.//' + W + 'br')), 1)
        self.assertFalse(root.findall('.//' + W + 'ins'))
        self.assertFalse(root.findall('.//' + W + 'del'))

    def test_literal_heading_text_and_fenced_examples_are_not_reinterpreted(self):
        document = ingest_bytes('paper.md', b'```\n# literal heading\n```\n\n# Real heading')
        root = xml(_minimal_docx(model_to_markdown(document), document=document))
        paragraphs = root.find(W + 'body').findall(W + 'p')
        self.assertEqual(text(paragraphs[0]), '```\n# literal heading\n```')
        self.assertIsNone(paragraphs[0].find(W + 'pPr'))
        self.assertEqual(paragraphs[1].find('.//' + W + 'outlineLvl').attrib[W + 'val'], '0')

    def test_normalized_project_export_retains_a_real_table_and_integrity(self):
        with tempfile.TemporaryDirectory() as temp:
            project = DocumentReviewProject.create(temp, filename='paper.docx', content=studio_fixture._docx())
            project.confirm_extraction('confirm')
            project.confirm_context(studio_fixture.DocumentReviewStudioTests().context())
            project.run_local_prechecks(['official_professional_format'])
            for finding in project.findings():
                project.decide_finding(finding.finding_id, 'reject', reason='Verify normalized export only')
            output = project.export()
            data = (output / 'normalized-editable-copy.docx').read_bytes()
            self.assertEqual(len(xml(data).findall('.//' + W + 'tbl')), 1)
            parsed = ingest_bytes('copy.docx', data)
            self.assertIn('合并单元格', parsed.plain_text)
            self.assertEqual(project.integrity_errors(), [])
