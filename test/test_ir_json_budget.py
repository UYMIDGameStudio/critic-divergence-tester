"""Expanded document budgets are exact, bounded, and preserve rejected originals."""
from dataclasses import replace
from pathlib import Path
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

from document_review_ingest import IngestionError, IngestionLimits, IR_SIZE_MESSAGE, IR_DATA_MESSAGE, ingest_bytes
from document_review_json_limits import JSONSizeLimitError, json_encoded_size
from document_review_model import canonical_json
from document_review_studio import DocumentReviewProject, ReviewStudioError
from project_lifecycle import _files
from studio_selftest import _ir_budget_fixture
from test.test_docx_notes import note_package
from test.test_office_formats import zipped, odt_entries, xlsx_entries, pptx_entries, MULTILINGUAL
from test.test_text_formats import rtf_unicode
from test.test_document_review_studio import _simple_pdf


class JSONBudgetTests(unittest.TestCase):
    def test_exact_canonical_bytes_for_escapes_unicode_numbers_and_shared_values(self):
        shared='English 简体中文 繁體中文 Straße Français 日本語 Русский Latīna 😀\u2028'
        controls=''.join(chr(n) for n in range(32))+'\x7f"\\'
        cases=[{},[],None,True,False,0,-100,1.0,-0.0,1e100,shared,controls,
            {'source':shared,'again':shared,'nested':[[shared,controls],{'x':None,'t':True,'f':False}]},
            {'cross-chunk':('a'*8190)+'\\"\n😀'+('b'*8190)}]
        for value in cases:
            expected=len(canonical_json(value))
            with self.subTest(kind=type(value).__name__):
                self.assertEqual(json_encoded_size(value,expected),expected)
                with self.assertRaises(JSONSizeLimitError):
                    json_encoded_size(value,expected-1)
        child=[shared,controls]
        self.assertEqual(json_encoded_size([child,child],100000),len(canonical_json([child,child])))

    def test_cycles_invalid_scalars_and_invalid_limits_cannot_enter_json_artifacts(self):
        cycle=[]
        cycle.append(cycle)
        for value in (cycle,{1:'not JSON-ready'},float('nan'),float('inf'),b'bytes','\ud800'):
            with self.subTest(kind=type(value).__name__), self.assertRaises(ValueError):
                json_encoded_size(value,10000)
        for maximum in (-1,True,1.5):
            with self.subTest(maximum=maximum), self.assertRaises(ValueError):
                json_encoded_size({},maximum)

    def test_control_character_expansion_is_rejected_without_allocating_escaped_json(self):
        value='\x00'*2_000_000
        tracemalloc.start()
        try:
            with self.assertRaises(JSONSizeLimitError):
                json_encoded_size(value,2_000_100)
            _,peak=tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak,1_000_000)


class IngestionJSONBudgetTests(unittest.TestCase):
    def test_all_current_format_routes_obey_the_same_exact_budget(self):
        cases=[('paper.txt',MULTILINGUAL.encode('utf-16')),
            ('paper.md',('# Article\n\n'+MULTILINGUAL).encode()),
            ('paper.html',('<p>'+MULTILINGUAL+'</p>').encode()),
            ('paper.rtf',('{\\rtf1\\ansi\\uc1 '+rtf_unicode(MULTILINGUAL)+'}').encode()),
            ('paper.csv',('Text,Value\n"'+MULTILINGUAL+'",3').encode()),
            ('paper.docx',note_package()),('paper.docm',note_package()),
            ('paper.odt',zipped(odt_entries())),('paper.xlsx',zipped(xlsx_entries())),
            ('paper.pptx',zipped(pptx_entries())),('paper.pdf',_simple_pdf())]
        for name,raw in cases:
            with self.subTest(name=name):
                original=ingest_bytes(name,raw)
                size=len(canonical_json(original.to_dict()))
                accepted=ingest_bytes(name,raw,limits=replace(IngestionLimits(),max_ir_json_bytes=size))
                self.assertEqual(canonical_json(accepted.to_dict()),canonical_json(original.to_dict()))
                with self.assertRaisesRegex(IngestionError,'解析结果展开后'):
                    ingest_bytes(name,raw,limits=replace(IngestionLimits(),max_ir_json_bytes=size-1))

    def test_nonfinite_or_invalid_adapter_data_is_blocked_before_writing(self):
        original=ingest_bytes('paper.txt',b'Visible body.')
        for value in (float('nan'),'\ud800'):
            original.metadata['invalid']=value
            with patch('document_review_ingest._ingest_bytes',return_value=original):
                with self.assertRaisesRegex(IngestionError,IR_DATA_MESSAGE):
                    ingest_bytes('paper.txt',b'Visible body.')
        for maximum in (-1,True):
            with patch('document_review_ingest._ingest_bytes') as parser:
                with self.assertRaises(IngestionError):
                    ingest_bytes('paper.txt',b'Visible body.',limits=replace(IngestionLimits(),max_ir_json_bytes=maximum))
                parser.assert_not_called()

    def test_default_budget_stops_a_small_amplifying_docx_and_retry_is_atomic(self):
        raw=_ir_budget_fixture()
        self.assertLess(len(raw),100000)
        with tempfile.TemporaryDirectory() as temp:
            project=DocumentReviewProject.create(temp,filename='expanded-notes.docx',content=raw)
            self.assertEqual(project.state()['extraction_state'],'blocked')
            self.assertFalse(project.document_path.exists())
            self.assertEqual(project.state()['diagnostics'],[IR_SIZE_MESSAGE])
            self.assertEqual((project.root/'source/expanded-notes.docx').read_bytes(),raw)
            self.assertFalse(project.can_review()[0])
            before={name:path.read_bytes() for name,path in _files(project.root).items()}
            with self.assertRaises((IngestionError,ReviewStudioError)):
                project.retry_extraction()
            self.assertEqual(before,{name:path.read_bytes() for name,path in _files(project.root).items()})
            self.assertEqual(project.integrity_errors(),[])

    def test_reparse_under_a_smaller_budget_preserves_the_earlier_project(self):
        raw=_ir_budget_fixture(paragraphs=128,path_length=8192)
        with tempfile.TemporaryDirectory() as temp:
            original=DocumentReviewProject.create(temp,filename='notes.docx',content=raw)
            before={name:path.read_bytes() for name,path in _files(original.root).items()}
            blocked=DocumentReviewProject.create(temp,filename='notes.docx',content=raw,new_project=True,
                limits=replace(IngestionLimits(),max_ir_json_bytes=1024*1024))
            self.assertNotEqual(original.root,blocked.root)
            self.assertFalse(blocked.document_path.exists())
            self.assertEqual(blocked.state()['extraction_state'],'blocked')
            self.assertEqual(before,{name:path.read_bytes() for name,path in _files(original.root).items()})


if __name__=='__main__':
    unittest.main()
