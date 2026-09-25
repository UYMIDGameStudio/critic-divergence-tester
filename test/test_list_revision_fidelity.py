"""Approved edits retain list identity without editing the immutable source IR."""
import json
import hashlib
import tempfile
import unittest
import zipfile
from xml.etree import ElementTree as ET

from document_review_model import ReviewContext
from document_review_studio import DocumentReviewProject


HTML = '<h1>Test</h1><ol start="7"><li><p>原则上 Alpha</p><p>Tail</p></li><li value="12">Next</li></ol>'
MARKDOWN = '# Test\n\n7. 原则上 Alpha\n\n12. Next'


class ListRevisionFidelityTests(unittest.TestCase):
    def revise(self, temp, *, source=HTML, operation='replace_block', after='明确 Alpha\n\nSupporting detail', decision='approve'):
        project = DocumentReviewProject.create(temp, filename='paper.md' if source == MARKDOWN else 'paper.html', content=source.encode())
        project.confirm_extraction('confirm')
        project.confirm_context(ReviewContext(document_type='memo', jurisdiction='unspecified',
            effective_date='2026-09-25', publisher_type='author', audience='reviewers').to_dict())
        original = project.document_path.read_bytes()
        project.run_local_prechecks(['expression_ambiguity'])
        target = next(b for b in project.document().blocks if b.text == '原则上 Alpha')
        accepted = False
        for finding in project.findings():
            selected = not accepted and finding.location.block_id == target.block_id
            project.decide_finding(finding.finding_id, 'accept' if selected else 'reject', reason='Review this paragraph only')
            accepted |= selected
        self.assertTrue(accepted)
        plan = project.prepare_revision_plan()
        self.assertEqual(len(plan['actions']), 1)
        action = plan['actions'][0]
        scope = [target.block_id, next(b.block_id for b in project.document().blocks if b.text == 'Tail')] if operation == 'replace_range' else None
        project.set_revision_action_operation(action['action_id'], operation, reason='Human selected this operation', block_ids=scope)
        hunk = project.propose_revision_hunk(action['action_id'], after, rationale='Explicit approved edit')
        project.decide_revision_hunk(hunk['hunk_id'], decision, reason='Checked the proposed edit')
        revision = project.finalize_revision()
        self.assertEqual(project.document_path.read_bytes(), original)
        self.assertEqual(project.integrity_errors(), [])
        saved = json.loads((revision / 'document.json').read_text(encoding='utf-8'))
        return project, revision, saved['blocks'], target

    def test_split_html_item_preserves_identity_and_only_one_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            project, revision, blocks, original = self.revise(temp)
            by_text = {b['text']: b for b in blocks}
            first, second, tail = (by_text[t] for t in ('明确 Alpha', 'Supporting detail', 'Tail'))
            self.assertEqual(first['attrs']['list_item_id'], second['attrs']['list_item_id'])
            self.assertEqual(first['attrs']['list_item_id'], tail['attrs']['list_item_id'])
            self.assertTrue(first['attrs']['list_item_start'])
            self.assertTrue(second['attrs']['list_continuation'])
            self.assertEqual(second['attrs']['list_marker'], '7.')
            self.assertEqual(second['attrs']['split_from_block_id'], original.block_id)
            self.assertNotIn('source_line', second['attrs'])
            self.assertEqual(second['location']['source_path'], 'generated')
            markdown = (revision / '修改稿.md').read_text(encoding='utf-8')
            self.assertEqual(markdown.count('7. '), 1)
            self.assertIn('12. Next', markdown)
            exported = project.export()
            with zipfile.ZipFile(exported / '修改稿.docx') as archive:
                root = ET.fromstring(archive.read('word/document.xml'))
            ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
            paragraphs = [''.join(t.text or '' for t in p.findall('.//w:t', ns)) for p in root.findall('./w:body/w:p', ns)]
            self.assertIn('7. 明确 Alpha', paragraphs)
            self.assertIn('Supporting detail', paragraphs)
            self.assertEqual(sum(text.startswith('7. ') for text in paragraphs), 1)
            self.assertEqual(project.integrity_errors(), [])

    def test_split_markdown_item_creates_shared_identity_without_inventing_numbers(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, blocks, _ = self.revise(temp, source=MARKDOWN)
            first, second = blocks[1:3]
            self.assertEqual(first['attrs']['list_item_id'], second['attrs']['list_item_id'])
            self.assertEqual(second['attrs']['list_marker'], '7.')
            self.assertTrue(first['attrs']['list_item_start'])
            self.assertFalse(second['attrs']['list_item_start'])

    def test_delete_first_fragment_promotes_remaining_fragment(self):
        with tempfile.TemporaryDirectory() as temp:
            _, revision, blocks, _ = self.revise(temp, operation='delete_block', after='')
            tail = next(b for b in blocks if b['text'] == 'Tail')
            self.assertEqual(tail['kind'], 'list_item')
            self.assertEqual(tail['location']['block_kind'], 'list_item')
            self.assertTrue(tail['attrs']['list_item_start'])
            self.assertFalse(tail['attrs']['list_continuation'])
            self.assertIn('7. Tail\n\n12. Next', (revision / '修改稿.md').read_text(encoding='utf-8'))

    def test_independent_insert_does_not_inherit_item_membership(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, blocks, _ = self.revise(temp, operation='insert_after', after='Independent paragraph')
            inserted = next(b for b in blocks if b['text'] == 'Independent paragraph')
            self.assertNotIn('list_item_id', inserted['attrs'])
            self.assertNotIn('list_marker', inserted['attrs'])

    def test_range_replacement_retains_item_identity_for_new_fragments(self):
        with tempfile.TemporaryDirectory() as temp:
            _, _, blocks, _ = self.revise(temp, operation='replace_range')
            self.assertNotIn('Tail', [b['text'] for b in blocks])
            self.assertEqual(blocks[1]['attrs']['list_item_id'], blocks[2]['attrs']['list_item_id'])
            self.assertTrue(blocks[1]['attrs']['list_item_start'])
            self.assertTrue(blocks[2]['attrs']['list_continuation'])

    def test_promoted_heading_keeps_its_level_and_item_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            _, revision, blocks, _ = self.revise(temp, source=HTML.replace('<p>Tail</p>', '<h2>Tail</h2>'), operation='delete_block', after='')
            tail = next(b for b in blocks if b['text'] == 'Tail')
            self.assertEqual((tail['kind'], tail['level']), ('heading', 2))
            self.assertTrue(tail['attrs']['list_item_start'])
            self.assertIn('## 7. Tail', (revision / '修改稿.md').read_text(encoding='utf-8'))

    def test_empty_split_fragments_do_not_consume_the_visible_item_start(self):
        with tempfile.TemporaryDirectory() as temp:
            _, revision, blocks, _ = self.revise(temp, after='\n\n明确 Alpha\n\nSupporting detail\n\n')
            first = next(b for b in blocks if b['text'] == '明确 Alpha')
            self.assertTrue(first['attrs']['list_item_start'])
            self.assertTrue(all(not b['attrs'].get('list_item_start') for b in blocks if not b['text'].strip()))
            self.assertIn('7. 明确 Alpha', (revision / '修改稿.md').read_text(encoding='utf-8'))

    def test_rejected_delete_retains_original_list_blocks(self):
        with tempfile.TemporaryDirectory() as temp:
            project, _, blocks, _ = self.revise(temp, operation='delete_block', after='', decision='reject')
            self.assertEqual(blocks, [b.to_dict() for b in project.document().blocks])

    def test_revision_source_map_covers_only_live_blocks(self):
        for operation, after in (('delete_block', ''), ('replace_block', 'New premise\n\nNew detail')):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as temp:
                _, revision, blocks, _ = self.revise(temp, source=HTML + '<table><tr><td>Cell</td></tr></table>', operation=operation, after=after)
                saved = json.loads((revision / 'document.json').read_text(encoding='utf-8'))
                self.assertEqual({row['block_id'] for row in saved['source_to_block']}, {b['block_id'] for b in blocks})

    def test_revision_map_distinguishes_generated_text_from_parent_anchors(self):
        with tempfile.TemporaryDirectory() as temp:
            project, revision, blocks, _ = self.revise(temp)
            saved = json.loads((revision / 'document.json').read_text(encoding='utf-8'))
            parent = saved['metadata']['source_map_parent']
            self.assertEqual(parent['sha256'], hashlib.sha256(project.document_path.read_bytes()).hexdigest())
            self.assertEqual(parent['relative_path'], project.document_path.relative_to(project.root).as_posix())
            self.assertEqual(saved['metadata']['character_offset_basis'], 'historical-parent-document')
            rows = {r['block_id']: r for r in saved['source_to_block']}
            by_text = {b['text']: rows[b['block_id']] for b in blocks}
            generated = by_text['Supporting detail']
            self.assertEqual(generated['coordinate_basis'], 'generated')
            self.assertIn('generated_by_action', generated)
            self.assertIn('split_from_block_id', generated)
            self.assertNotIn('source_line', generated)
            self.assertNotIn('char_start', generated)
            self.assertTrue(by_text['明确 Alpha']['text_changed_from_parent'])
            self.assertFalse(by_text['Tail']['text_changed_from_parent'])
            self.assertEqual(by_text['Tail']['coordinate_basis'], 'parent-document-anchor')
