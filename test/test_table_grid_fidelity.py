"""Logical table columns survive import, approved revision and native export."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape

import document_review_ingest as ingest
from document_review_model import ReviewContext, model_to_markdown
from document_review_studio import DocumentReviewProject, ReviewStudioError
from document_review_word import WordEditUnsupported, preserve_docx
from project_lifecycle import _files
from test.test_document_review_word import package, paragraph, xml, rendered_text, NS

OLD = '相关人员及时完成报名。'
NEW = '负责人周五前完成报名。'


def cell(text, span=None):
    props = '' if span is None else f'<w:tcPr><w:gridSpan w:val="{escape(str(span))}"/></w:tcPr>'
    return '<w:tc>' + props + paragraph(text) + '</w:tc>'


def row(cells, before=0, after=0):
    props = f'<w:trPr><w:gridBefore w:val="{before}"/><w:gridAfter w:val="{after}"/></w:trPr>'
    return '<w:tr>' + props + ''.join(cells) + '</w:tr>'


def table(rows, columns=3):
    grid = '' if columns is None else '<w:tblGrid>' + '<w:gridCol w:w="1000"/>' * columns + '</w:tblGrid>'
    return '<w:tbl>' + grid + ''.join(rows) + '</w:tbl>'


class TableGridFidelityTests(unittest.TestCase):
    def prepare(self, root, filename, raw):
        project = DocumentReviewProject.create(root, filename=filename, content=raw)
        project.confirm_extraction('confirm')
        project.confirm_context(ReviewContext('document', 'unknown', 'unknown', 'author', 'readers').to_dict())
        project.run_local_prechecks(['expression_ambiguity'])
        target = next(b for b in project.document().blocks if b.kind == 'table_cell' and b.text == OLD)
        finding = next(f for f in project.findings() if f.location.block_id == target.block_id)
        for item in project.findings():
            project.decide_finding(item.finding_id, 'accept' if item.finding_id == finding.finding_id else 'reject', reason='Synthetic explicit scope')
        action = project.prepare_revision_plan()['actions'][0]
        project.set_revision_action_operation(action['action_id'], 'replace_table_cell', reason='Edit the selected cell only')
        hunk = project.propose_revision_hunk(action['action_id'], NEW, rationale='Specify the actor and deadline')
        project.decide_revision_hunk(hunk['hunk_id'], 'approve', reason='Explicit fixture approval')
        return project, target

    def test_word_offsets_spans_and_padding_share_logical_columns(self):
        raw = package(table([row([cell('Group', 2), cell('Result')], before=1, after=1),
                             row([cell('A'), cell('B'), cell('C'), cell('D'), cell('E')])], columns=5))
        document = ingest.ingest_bytes('table.docx', raw)
        container = next(b for b in document.blocks if b.kind == 'table')
        self.assertEqual(container.attrs['rows'], [['', 'Group', '', 'Result', ''], ['A', 'B', 'C', 'D', 'E']])
        self.assertEqual(container.attrs['grid_layout'], 'logical-columns')
        first = [b for b in document.blocks if b.kind == 'table_cell' and b.location.row == 0]
        self.assertEqual([(b.text, b.location.column) for b in first], [('Group', 1), ('Result', 3)])
        self.assertTrue(any(w.code == 'docx-table-grid-flattened' for w in document.warnings))
        reparsed = ingest.ingest_bytes('table.md', model_to_markdown(document).encode())
        self.assertEqual(next(b.attrs['rows'] for b in reparsed.blocks if b.kind == 'table'), container.attrs['rows'])

    def test_span_can_augment_a_smaller_declared_grid_without_losing_columns(self):
        raw = package(table([row([cell('Wide', '+003')])], columns=1))
        document = ingest.ingest_bytes('table.docx', raw)
        self.assertEqual(next(b.attrs['rows'] for b in document.blocks if b.kind == 'table'), [['Wide', '', '']])

    def test_conflicting_trailing_offsets_do_not_invent_grid_columns(self):
        for after in (2, 5):
            with self.subTest(after=after):
                raw = package(table([row([cell('A'), cell('B')], after=after)], columns=3))
                document = ingest.ingest_bytes('table.docx', raw)
                self.assertEqual(next(b.attrs['rows'] for b in document.blocks if b.kind == 'table'), [['A', 'B', '']])

    def test_invalid_geometry_is_rejected_and_unsupported_offsets_are_explicit(self):
        for value in ('0', '-1', 'text', '1.5', '9' * 100):
            with self.subTest(value=value), self.assertRaises(ingest.IngestionError):
                ingest.ingest_bytes('table.docx', package(table([row([cell('A', value), cell('B')])])))
        for columns in (None, 1):
            with self.subTest(columns=columns), self.assertRaisesRegex(ingest.IngestionError, '暂不支持'):
                ingest.ingest_bytes('table.docx', package(table([row([cell('A')], before=2)], columns=columns)))

    def test_table_grid_and_cell_revision_marks_cannot_look_like_a_clean_draft(self):
        base = table([row([cell('A')])], columns=1)
        variants = [base.replace('</w:tblGrid>', '<w:tblGridChange w:id="1"><w:tblGrid><w:gridCol/></w:tblGrid></w:tblGridChange></w:tblGrid>')]
        for tag in ('cellIns', 'cellDel', 'cellMerge'):
            variants.append(base.replace('<w:tc>', '<w:tc><w:tcPr><w:' + tag + ' w:id="1"/></w:tcPr>'))
        variants.append(base.replace('<w:tr>', '<w:tr><w:tblPrEx><w:tblPrExChange w:id="1"><w:tblPrEx/></w:tblPrExChange></w:tblPrEx>'))
        for body in variants:
            with self.subTest(body=body[:80]):
                raw = package(body)
                document = ingest.ingest_bytes('table.docx', raw)
                self.assertTrue(document.metadata['revisions_present'])
                self.assertIn('unaccepted-revisions', [warning.code for warning in document.warnings])
                document.metadata['revisions_present'] = False
                with self.assertRaises(WordEditUnsupported):
                    preserve_docx(raw, document, document)

    def test_expanded_rectangles_and_all_tables_obey_resource_limits(self):
        merged = table([row([cell('A', 3)]), row([cell('B', 3)])])
        for changes in ({'max_office_rows': 1}, {'max_office_columns': 2}, {'max_office_cells': 5}):
            with self.subTest(changes=changes), self.assertRaises(ingest.IngestionError):
                ingest.ingest_bytes('table.docx', package(merged), limits=replace(ingest.IngestionLimits(), **changes))
        one = table([row([cell('A', 3)])])
        with self.assertRaises(ingest.IngestionError):
            ingest.ingest_bytes('table.docx', package(one + one), limits=replace(ingest.IngestionLimits(), max_office_cells=5))
        for invalid in (True, 0, -1, float('nan')):
            with self.subTest(invalid=invalid), self.assertRaises(ingest.IngestionError):
                ingest.ingest_bytes('table.docx', package(one), limits=replace(ingest.IngestionLimits(), max_office_cells=invalid))
        with tempfile.TemporaryDirectory() as temp:
            raw = package(merged)
            project = DocumentReviewProject.create(temp, filename='table.docx', content=raw,
                limits=replace(ingest.IngestionLimits(), max_office_cells=5))
            self.assertFalse(project.document_path.exists())
            self.assertEqual((project.root / 'source/table.docx').read_bytes(), raw)

    def test_html_colspan_and_rowspan_edits_use_logical_not_sibling_columns(self):
        examples = [('<table><tr><td colspan="2">Fixed</td><td>' + OLD + '</td></tr></table>', 0, 2),
                    ('<table><tr><td rowspan="2">Fixed</td><td>Other</td></tr><tr><td>' + OLD + '</td></tr></table>', 1, 1)]
        for html, expected_row, expected_column in examples:
            with self.subTest(row=expected_row), tempfile.TemporaryDirectory() as temp:
                project, target = self.prepare(temp, 'table.html', html.encode())
                original = deepcopy(next(b.attrs['rows'] for b in project.document().blocks if b.kind == 'table'))
                output = project.finalize_revision()
                value = json.loads((output / 'document.json').read_text(encoding='utf-8'))
                actual = next(b['attrs']['rows'] for b in value['blocks'] if b['kind'] == 'table')
                original[expected_row][expected_column] = NEW
                self.assertEqual(actual, original)
                self.assertNotIn(OLD, (output / '修改稿.md').read_text(encoding='utf-8'))
                self.assertEqual((target.location.row, target.location.column), (expected_row, expected_column))
                self.assertEqual(project.integrity_errors(), [])

    def test_word_native_edit_targets_the_right_cell_after_omissions_and_a_merge(self):
        raw = package(table([row([cell('Unchanged', 2), cell(OLD)], before=1, after=1)], columns=5))
        with tempfile.TemporaryDirectory() as temp:
            project, target = self.prepare(temp, 'table.docx', raw)
            self.assertEqual(target.location.column, 3)
            output = project.finalize_revision()
            exported = project.export()
            native = (exported / '修改稿.docx').read_bytes()
            nodes = xml(native).findall('w:body/w:tbl/w:tr/w:tc', NS)
            self.assertEqual([rendered_text(node) for node in nodes], ['Unchanged', NEW])
            reparsed = ingest.ingest_bytes('native.docx', native)
            self.assertEqual(next(b.attrs['rows'] for b in reparsed.blocks if b.kind == 'table'), [['', 'Unchanged', '', NEW, '']])
            self.assertEqual((project.root / 'source/table.docx').read_bytes(), raw)
            legacy = project.document()
            for block in legacy.blocks:
                if block.kind == 'table_cell':
                    block.location = replace(block.location, column=block.location.column - 1)
            with self.assertRaises(WordEditUnsupported):
                preserve_docx(raw, legacy, legacy)

    def test_historical_compact_word_rows_keep_their_original_storage(self):
        raw = package(table([row([cell('Group', 2), cell(OLD)])]))

        def legacy_parse(*args, **kwargs):
            document = ingest.ingest_bytes(*args, **kwargs)
            for block in document.blocks:
                if block.kind == 'table':
                    cells = [document.block(identity) for identity in block.children]
                    block.attrs.pop('grid_layout')
                    block.attrs['rows'] = [[cell.text for cell in cells]]
            document.parser_version = 'document-review-studio-docx-v2'
            return document

        with tempfile.TemporaryDirectory() as temp:
            with patch('document_review_studio.ingest_bytes', side_effect=legacy_parse):
                project, target = self.prepare(temp, 'table.docx', raw)
            original = project.document_path.read_bytes()
            output = project.finalize_revision()
            value = json.loads((output / 'document.json').read_text(encoding='utf-8'))
            self.assertEqual(next(b['attrs']['rows'] for b in value['blocks'] if b['kind'] == 'table'), [['Group', NEW]])
            self.assertEqual(project.document_path.read_bytes(), original)
            self.assertEqual(target.location.column, 2)
            self.assertTrue((project.export() / '修改稿.md').is_file())
            self.assertEqual(project.integrity_errors(), [])

    def test_historical_wrong_column_revision_cannot_be_exported_as_trusted(self):
        raw = ('<table><tr><td colspan="2">Fixed</td><td>' + OLD + '</td></tr></table>').encode()

        def historical_renderer(document):
            # Reproduce the old materializer, with valid receipts: the cell
            # block was updated, but the grid wrote into its sibling index.
            if any(b.kind == 'table_cell' and b.text == NEW for b in document.blocks):
                container = next(b for b in document.blocks if b.kind == 'table')
                container.attrs['rows'][0][1:] = [NEW, OLD]
            return model_to_markdown(document)

        with tempfile.TemporaryDirectory() as temp:
            project, _ = self.prepare(Path(temp) / 'historical', 'table.html', raw)
            with patch('document_review_studio.model_to_markdown', side_effect=historical_renderer):
                historical = project.finalize_revision()
            self.assertIn('| Fixed | ' + NEW + ' | ' + OLD + ' |', (historical / '修改稿.md').read_text(encoding='utf-8'))
            self.assertEqual(project.integrity_errors(), [])
            before = {name: file.read_bytes() for name, file in _files(project.root).items()}
            with self.assertRaisesRegex(ReviewStudioError, '重新解析为新项目'):
                project.export()
            self.assertEqual({name: file.read_bytes() for name, file in _files(project.root).items()}, before)
            self.assertTrue((historical / '修改稿.md').is_file())
            # The suggested recovery works without modifying old evidence.
            fresh, _ = self.prepare(Path(temp) / 'reparsed', 'table.html', raw)
            fresh.finalize_revision()
            markdown = (fresh.export() / '修改稿.md').read_text(encoding='utf-8')
            self.assertIn('| Fixed |  | ' + NEW + ' |', markdown)
            self.assertNotIn(OLD, markdown)
            self.assertEqual({name: file.read_bytes() for name, file in _files(project.root).items()}, before)

    def test_inconsistent_or_unknown_grid_stops_before_any_revision_is_committed(self):
        with tempfile.TemporaryDirectory() as temp:
            project, target = self.prepare(temp, 'table.html', ('<table><tr><td colspan="2">Fixed</td><td>' + OLD + '</td></tr></table>').encode())
            path, document = project._review_document_record()
            before = {name: file.read_bytes() for name, file in _files(project.root).items()}
            for mode in ('wrong-text', 'missing-child', 'future-layout'):
                altered = deepcopy(document)
                container = next(b for b in altered.blocks if b.kind == 'table')
                if mode == 'wrong-text':
                    container.attrs['rows'][0][2] = 'A different source cell'
                elif mode == 'missing-child':
                    container.children.remove(target.block_id)
                else:
                    container.attrs['grid_layout'] = 'future-layout'
                with self.subTest(mode=mode), patch.object(project, '_review_document_record', return_value=(path, altered)):
                    with self.assertRaises(ReviewStudioError):
                        project.finalize_revision()
                self.assertEqual({name: file.read_bytes() for name, file in _files(project.root).items()}, before)
