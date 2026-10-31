"""Owned HTML preserves source declarations without visiting destinations."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from document_review_ingest import ingest_bytes, IngestionError, IngestionLimits
from document_review_model import document_location_contract, has_reviewable_text, ReviewContext
from document_review_studio import DocumentReviewProject


def parse(text):
    return ingest_bytes('paper.html', text.encode('utf-8'), encoding='utf-8')


def references(document):
    return [(block, record) for block in document.blocks for record in block.attrs.get('hyperlink_references', [])]


class HTMLHyperlinkTests(unittest.TestCase):
    def test_labels_and_targets_stay_separate_across_paragraph_list_and_table(self):
        document = parse('<p>Read <a href="https://example.org/study?x=1&amp;y=2">the <em>study</em></a>.</p>'
            '<ol start="4"><li><a href="appendix.html#scope">Appendix</a></li></ol>'
            '<table><caption><a href="caption.pdf">Caption source</a></caption><tr><td><a href="#result">Result</a></td></tr></table>')
        rows = references(document)
        self.assertEqual([record['target'] for block, record in rows], ['https://example.org/study?x=1&y=2', 'appendix.html#scope', 'caption.pdf', '#result'])
        self.assertEqual([record['label'] for block, record in rows], ['the study', 'Appendix', 'Caption source', 'Result'])
        self.assertEqual([block.kind for block, record in rows], ['paragraph', 'list_item', 'paragraph', 'table_cell'])
        self.assertEqual(document.blocks[0].text, 'Read the study.')
        self.assertNotIn('https://', document.plain_text)
        self.assertEqual(document.metadata['html_hyperlink_visibility']['reference_count'], 4)

    def test_one_anchor_across_blocks_has_one_identity_and_distinct_label_segments(self):
        document = parse('<a href="paper.pdf"><h2>Methods</h2><p>Limited scope</p><table><tr><td>Evidence</td></tr></table></a><p>Outside</p>')
        rows = references(document)
        self.assertEqual([record['label'] for block, record in rows], ['Methods', 'Limited scope', 'Evidence'])
        self.assertEqual(len({record['link_id'] for block, record in rows}), 1)
        self.assertNotIn('hyperlink_references', document.blocks[-1].attrs)
        self.assertEqual(document.metadata['html_hyperlink_visibility']['declaration_count'], 1)

    def test_hidden_and_embedded_content_cannot_supply_labels_or_targets(self):
        document = parse('<template><a href="secret">hidden</a></template><svg><a href="svg">hidden</a></svg>'
            '<p><a href="study">Read <span hidden>SECRET</span><b>evidence</b><script>bad()</script></a></p>')
        rows = references(document)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1]['label'], 'Read evidence')
        self.assertEqual(rows[0][1]['target'], 'study')

    def test_base_and_duplicate_attributes_are_retained_without_url_resolution(self):
        document = parse('<head><base href="https://example.org/articles/"><base href="file:///private/"></head>'
            '<template><base href="https://hidden.example/"></template>'
            '<p><a href="../study" href="https://other.example">Study</a></p>')
        visibility = document.metadata['html_hyperlink_visibility']
        self.assertEqual([base['target'] for base in visibility['base_declarations']], ['https://example.org/articles/', 'file:///private/'])
        self.assertEqual(visibility['url_resolution'], 'not-performed')
        self.assertFalse(visibility['fetched'])
        record = references(document)[0][1]
        self.assertEqual(record['target'], '../study')
        self.assertEqual(record['href_declarations'], ['../study', 'https://other.example'])
        self.assertTrue(any(w.code == 'html-link-attributes-ambiguous' for w in document.warnings))

    def test_empty_links_do_not_create_body_text_or_guess_an_address(self):
        document = parse('<a href></a><a href=""></a><a id="bookmark"></a>')
        self.assertEqual([record['target'] for block, record in references(document)], ['', ''])
        self.assertFalse(has_reviewable_text(document))
        self.assertEqual(document.plain_text, '')

    def test_nested_and_unclosed_anchors_do_not_leak_into_later_text(self):
        nested = parse('<p><a href="outer">Outer<a href="inner">Inner</a>Tail</a></p><p>Outside</p>')
        self.assertEqual([record['label'] for block, record in references(nested)], ['Outer', 'Inner'])
        self.assertTrue(any(w.code == 'html-link-structure-uncertain' for w in nested.warnings))
        unclosed = parse('<p><a href="one">First</p><p>Outside</p><a href="two">Last')
        self.assertEqual([record['label'] for block, record in references(unclosed)], ['First', 'Last'])

    def test_long_labels_are_bounded_previews_but_body_and_targets_remain_intact(self):
        text = '字' * 700
        document = parse('<p><a href="study">' + text + '</a></p>')
        block, record = references(document)[0]
        self.assertEqual(block.text, text)
        self.assertEqual(record['label'], text[:512])
        self.assertTrue(record['label_truncated'])
        for text in (' ' * 800 + 'short', 'short' + ' ' * 800):
            record = references(parse('<pre><a href="study">' + text + '</a></pre>'))[0][1]
            self.assertEqual(record['label'], 'short')
            self.assertFalse(record['label_truncated'])

    def test_caption_only_tables_and_cell_boundaries_keep_link_associations(self):
        caption = parse('<table><caption><a href="source">Caption</a></caption></table>')
        self.assertEqual(references(caption)[0][0].text, 'Caption')
        document = parse('<table><tr><td><a href="one">One<td>Outside<td><a href="two">Two</table>')
        self.assertEqual([record['label'] for block, record in references(document)], ['One', 'Two'])
        self.assertFalse(next(block for block in document.blocks if block.text == 'Outside').attrs.get('hyperlink_references'))

    def test_limits_reject_without_losing_original_or_returning_partial_ir(self):
        raw = b'<p><a href="study">Body</a></p>'
        with self.assertRaises(IngestionError):
            ingest_bytes('p.html', raw, limits=IngestionLimits(max_ir_json_bytes=128))
        with patch('document_review_text_formats.MAX_BLOCKS', 2):
            self.assertEqual(len(references(parse('<p><a href="a">A</a><a href="b">B</a></p>'))), 2)
            with self.assertRaises(IngestionError):
                parse('<p><a href="a">A</a><a href="b">B</a><a href="c">C</a></p>')
        with tempfile.TemporaryDirectory() as directory:
            project = DocumentReviewProject.create(Path(directory), filename='p.html', content=raw, limits=IngestionLimits(max_ir_json_bytes=128))
            self.assertIsNone(project.document())
            self.assertEqual((project.root/'source/p.html').read_bytes(), raw)

    def test_eight_language_labels_and_addresses_keep_declared_encoding(self):
        for codec, label in [('utf-8','English'), ('gb18030','简体中文'), ('big5','繁體中文'),
                ('cp1252','Deutsch Straße'), ('cp1252','Français été'), ('cp932','日本語'),
                ('cp1251','Русский'), ('utf-16','Latīna Rōma')]:
            with self.subTest(codec=codec, label=label):
                raw = ('<p><a href="https://example.org/' + label + '">' + label + '</a></p>').encode(codec)
                document = ingest_bytes('p.html', raw, encoding=codec)
                block, record = references(document)[0]
                self.assertEqual(block.text, label)
                self.assertEqual(record['label'], label)
                self.assertEqual(record['target'], 'https://example.org/' + label)

    def test_unassociated_table_links_are_retained_as_declarations_without_invented_text(self):
        document = parse('<table><a href="unexpanded">No cell</a><tr><td>Body</td></tr></table>')
        visibility = document.metadata['html_hyperlink_visibility']
        self.assertEqual(visibility['unassociated_declarations'], 1)
        self.assertEqual(visibility['unassociated_links'][0]['target'], 'unexpanded')
        self.assertNotIn('No cell', document.plain_text)
        self.assertTrue(any(w.code == 'html-links-unassociated' for w in document.warnings))

    def test_academic_request_retains_declarations_with_origin_and_verification_limits(self):
        raw = '<head><base href="https://example.org/"></head><p><a href="study">English 简体中文 繁體中文 Deutsch Français 日本語 Русский Latīna</a></p>'
        with patch('socket.create_connection', side_effect=AssertionError('No network')), tempfile.TemporaryDirectory() as directory:
            project = DocumentReviewProject.create(directory, filename='p.html', content=raw.encode())
            project.confirm_extraction('confirm')
            project.confirm_context(ReviewContext('paper', 'unknown', 'unknown', 'author', 'researchers', review_profile='academic', research_type='theoretical').to_dict())
            request = project.prepare_ai_audits(['academic_argument'], provider='fixture', model='not-invoked')[0]
            self.assertIn('base_declarations', request['prompt'])
            self.assertIn('not-performed', request['prompt'])
            self.assertIn('not independent verification', request['prompt'])
            document = project.document()
            document.metadata['revision_of'] = 'earlier'
            self.assertIn('historical', document_location_contract(document)['hyperlinks'])
            # Exercise the saved defense and revised-document paths, not only
            # the common helper used by ordinary academic requests.
            old, new = '相关人员及时完成报名。', '负责人周五前完成报名。'
            original = ('<head><base href="https://example.org/"></head><p><a href="study">' + old + '</a></p>').encode()
            editing = DocumentReviewProject.create(directory, filename='edit.html', content=original)
            editing.confirm_extraction('confirm')
            editing.confirm_context(ReviewContext('document', 'unknown', 'unknown', 'author', 'readers').to_dict())
            editing.run_local_prechecks(['expression_ambiguity'])
            from test import test_review_round_protocols as fixtures
            helper = fixtures.ReviewRoundProtocolTests()
            helper.project = editing
            request = editing.prepare_ai_audits(['expression_ambiguity'], provider='fixture', model='not-invoked')[0]
            finding = helper.finding(block=editing.document().blocks[0])
            finding.update(critic='expression_ambiguity', document_type='document')
            run = editing.collect_model_audit('expression_ambiguity', json.dumps(helper.response(request, finding), ensure_ascii=False),
                provider='fixture', model='not-invoked', request_id=request['request_id'])
            findings = editing.findings()
            defense = editing.prepare_adversarial_review(run.findings[0].finding_id, provider='fixture', model='not-invoked')
            self.assertIn('base_declarations', defense['requests'][0]['prompt'])
            for finding in findings:
                editing.decide_finding(finding.finding_id, 'accept' if finding.finding_id == run.findings[0].finding_id else 'reject', reason='Owned fixture scope')
            action = editing.prepare_revision_plan()['actions'][0]['action_id']
            editing.set_revision_action_operation(action, 'replace_block', reason='Explicit fixture operation')
            hunk = editing.propose_revision_hunk(action, new, rationale='Owned fixture repair')
            editing.decide_revision_hunk(hunk['hunk_id'], 'approve', reason='Explicit fixture approval')
            output = editing.finalize_revision()
            revised_records = [json.loads(path.read_text(encoding='utf-8')) for path in output.rglob('*.json')]
            revised = next(row for row in revised_records if 'blocks' in row and row.get('metadata', {}).get('revision_of'))
            self.assertEqual(revised['metadata']['html_hyperlink_visibility']['base_declarations'][0]['target'], 'https://example.org/')
            self.assertEqual(next(b for b in revised['blocks'] if b['text'] == new)['attrs']['hyperlink_references'][0]['target'], 'study')
            recheck_prompts = [path.read_text(encoding='utf-8') for path in output.rglob('*.md') if 'base_declarations' in path.read_text(encoding='utf-8')]
            self.assertTrue(recheck_prompts)
            self.assertTrue(any('historical source declarations' in prompt for prompt in recheck_prompts))
            self.assertEqual((editing.root/'source/edit.html').read_bytes(), original)
            self.assertFalse(editing.integrity_errors())


if __name__ == '__main__':
    unittest.main()
