"""Numbered premises must retain their identifiers through review and export."""
import io
import json
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree as ET

from document_review_ingest import ingest_bytes
from document_review_model import ReviewContext, model_to_markdown
from document_review_studio import DocumentReviewProject, _minimal_docx


def word_paragraphs(data):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        root = ET.fromstring(archive.read('word/document.xml'))
    ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    return [''.join(node.text or '' for node in p.findall('.//w:t', ns))
            for p in root.findall('./w:body/w:p', ns)]


class ListMarkerFidelityTests(unittest.TestCase):
    def test_ir_and_markdown_keep_original_identifiers_in_eight_languages(self):
        texts = ['Premise', '前提简体', '前提繁體', 'Prämisse', 'Prémisse', '前提です', 'Посылка', 'Praemissa']
        markers = ['3.', '7)', '012.', '0)', '*', '+', '-', '１２.']
        source = '\r\n'.join(f'{marker} {text}' for marker, text in zip(markers, texts))
        for name in ('paper.md', 'paper.txt'):
            with self.subTest(name=name):
                document = ingest_bytes(name, source.encode())
                self.assertEqual([b.attrs.get('list_marker') for b in document.blocks], markers)
                self.assertEqual([b.text for b in document.blocks], texts)
                for block, line in zip(document.blocks, source.splitlines()):
                    self.assertEqual(source[block.location.char_start:block.location.char_end], line)
                markdown = model_to_markdown(document)
                self.assertEqual(markdown.splitlines()[::2], source.splitlines())
                restored = ingest_bytes(name, markdown.encode())
                self.assertEqual([b.attrs.get('list_marker') for b in restored.blocks], markers)

    def test_both_word_export_paths_keep_skipped_and_restarted_numbers(self):
        source = '3. First premise\n7) Exception\n12. Conclusion\n1. New sequence'
        document = ingest_bytes('paper.md', source.encode())
        for data in (_minimal_docx(source), _minimal_docx(model_to_markdown(document), document=document)):
            self.assertEqual(word_paragraphs(data), source.splitlines())

    def test_invalid_or_legacy_markers_use_safe_compatible_fallbacks(self):
        document = ingest_bytes('paper.md', b'3. Claim\n* Note')
        for marker in (None, '', '3.\n# Injected', '<script>', 3, {}, '3.x', '4. '):
            with self.subTest(marker=marker):
                for block in document.blocks:
                    block.attrs['list_marker'] = marker
                self.assertEqual(model_to_markdown(document), '1. Claim\n\n- Note\n')
                self.assertEqual(word_paragraphs(_minimal_docx('', document=document)), ['1. Claim', '- Note'])
        document.blocks[0].attrs['list_marker'] = '-'
        document.blocks[1].attrs['list_marker'] = '9.'
        self.assertEqual(model_to_markdown(document), '1. Claim\n\n- Note\n')
        for block in document.blocks:
            del block.attrs['list_marker']
        self.assertEqual(model_to_markdown(document), '1. Claim\n\n- Note\n')

    def test_fenced_examples_remain_literal(self):
        source = '```text\n3. Literal example\n7) Literal example\n```'
        document = ingest_bytes('paper.md', source.encode())
        self.assertEqual(len(document.blocks), 1)
        self.assertNotIn('list_marker', document.blocks[0].attrs)
        self.assertEqual(model_to_markdown(document), source + '\n')

    def test_review_prompt_contains_source_number_and_project_remains_valid(self):
        with tempfile.TemporaryDirectory() as temp:
            project = DocumentReviewProject.create(temp, filename='paper.md', content=b'7) Explicit premise')
            project.confirm_extraction('confirm')
            project.confirm_context(ReviewContext(document_type='memo', jurisdiction='unspecified',
                                    effective_date='2026-09-25', publisher_type='author', audience='reviewers').to_dict())
            prompt = project.prompt('expression_ambiguity')
            payload = prompt.split('## Internal document blocks\n```json\n', 1)[1].split('\n```', 1)[0]
            block = json.loads(payload)[0]
            self.assertEqual(block['attrs']['list_marker'], '7)')
            self.assertEqual(block['text'], 'Explicit premise')
            self.assertEqual(project.integrity_errors(), [])
