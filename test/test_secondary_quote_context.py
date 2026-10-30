"""Derived secondary evidence uses its bound IR, never model-supplied previews."""
import copy
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch

import document_review_quality as quality
import document_review_studio as studio
from test.test_quote_context_review import reviewed_fixture
from test import test_argument_composition, test_adversarial_review, test_resolution_evidence


class SecondaryQuoteContextTests(unittest.TestCase):
    def helper(self, kind):
        helper = kind()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        return helper

    def assert_bound(self, review, document, block_id):
        self.assertEqual(review['document_id'], document.document_id)
        self.assertEqual(review['block_id'], block_id)
        self.assertEqual(review['block_text_sha256'], hashlib.sha256(document.block(block_id).text.encode()).hexdigest())
        self.assertEqual(review['semantic_accuracy'], 'not-established')

    def test_shared_index_deduplicates_and_explicitly_limits_uninspected_previews(self):
        with tempfile.TemporaryDirectory() as temp:
            project, _ = reviewed_fixture(temp)
            document = project.document()
            block = document.blocks[0]
            index = quality.QuoteReviewIndex(document, max_previews=1)
            anchor = {'block_id': block.block_id, 'quote': 'can'}
            first = index.reference(anchor)
            with patch.object(quality, '_quote_review', side_effect=AssertionError('must not rescan')):
                self.assertEqual(index.reference(anchor), first)
                limited = index.reference({**anchor, 'quote': '5%'})
                self.assertEqual(index.reference({**anchor, 'quote': 'approved'}), limited)
            self.assertEqual(len(index.catalog), 2)
            self.assertIsNone(index.catalog[limited]['matched'])
            self.assertEqual(index.catalog[limited]['flags'], ['projection-limited'])
            self.assertIn('not inspected', '\n'.join(quality.quote_review_markdown(index.catalog[limited])))
            self.assertIn('partial-token', index.catalog[first]['flags'])
            self.assert_bound(index.catalog[first], document, block.block_id)
            for invalid in (-1, True, 1.5, '1'):
                with self.assertRaises(ValueError):
                    quality.QuoteReviewIndex(document, max_previews=invalid)
            with self.assertRaises(ValueError):
                quality.source_quote_reviews(project.document(), [], index=index)

    def test_changed_block_cannot_reuse_an_earlier_preview(self):
        with tempfile.TemporaryDirectory() as temp:
            project, _ = reviewed_fixture(temp)
            document = project.document()
            block = document.blocks[0]
            index = quality.QuoteReviewIndex(document)
            anchor = {'block_id': block.block_id, 'quote': 'can'}
            old = index.reference(anchor)
            block.text = 'We can now inspect the replacement.'
            new = index.reference(anchor)
            self.assertNotEqual(new, old)
            self.assertNotEqual(index.catalog[new]['block_text_sha256'], index.catalog[old]['block_text_sha256'])
            self.assertFalse(index.catalog[new]['flags'])

    def test_close_reading_refs_reuse_primary_context_and_preserve_raw_audits(self):
        from test.test_close_reading_quality import CloseReadingQualityTests
        helper = self.helper(CloseReadingQualityTests)
        request = helper.helper.request()
        finding = helper.helper.finding()
        detail = helper.detail()
        block = next(b for b in helper.project.document().blocks if 'English' in b.text)
        detail['context_evidence'] = [{'block_id': block.block_id, 'quote': 'nglish', 'role': 'qualification'}]
        finding['location'] = {'block_id': block.block_id}
        finding['evidence'] = 'nglish'
        finding['check_data'] = {'close_reading': detail, 'context_quote_refs': ['MODEL-SPOOF']}
        run = helper.helper.collect(request, helper.helper.response(request, finding))
        path = helper.project.root / 'audits'
        before = {p: p.read_bytes() for p in path.rglob('*') if p.is_file()}
        view = helper.project.view()
        refs = view['verification_context'][run.findings[0].finding_id]['context_quote_refs']
        self.assertEqual(view['quote_contexts'][refs[0]], view['verification_context'][run.findings[0].finding_id]['source_quote_review'])
        self.assertNotEqual(refs[0], 'MODEL-SPOOF')
        self.assertIn('partial-token', view['quote_contexts'][refs[0]]['flags'])
        folder = helper.project.export_ai_reviews()
        snapshot = json.loads((folder / 'AI审查结果.json').read_text(encoding='utf-8'))
        self.assertEqual(snapshot['runs'][0]['findings'][0]['check_data'], finding['check_data'])
        self.assertEqual(snapshot['quote_contexts'], view['quote_contexts'])
        self.assertIn('nglish', next(folder.glob('*.md')).read_text(encoding='utf-8'))
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_zero_finding_composition_has_frame_and_dimension_contexts_and_historical_labels(self):
        helper = self.helper(test_argument_composition.ArgumentCompositionTests)
        request = helper.request()
        payload = helper.payload(request)
        assessment = payload['argument_assessment']
        anchor = {**assessment['review_frame']['evidence'][0], 'quote': 'pretations'}
        assessment['review_frame']['evidence'] = [anchor]
        for axis in assessment['dimensions']:
            axis['evidence'] = [copy.deepcopy(anchor)]
        run = helper.collect(request, payload)
        view = helper.project.view()
        row = view['argument_assessments'][0]
        refs = row['quote_refs']
        identity = refs['review_frame'][0]
        self.assertTrue(all(pair == [identity] for pair in refs['dimensions']))
        self.assertEqual(row['assessment'], assessment)
        self.assertIn('partial-token', view['quote_contexts'][identity]['flags'])
        folder = helper.project.export_ai_reviews()
        snapshot = json.loads((folder / 'AI审查结果.json').read_text(encoding='utf-8'))
        self.assertEqual(snapshot['argument_quote_refs'][run.run_id], refs)
        # A later independent run replaces the active assessment, not its immutable history.
        request2 = helper.request()
        second = helper.collect(request2, helper.payload(request2))
        final = helper.project.export()
        audit = json.loads((final / 'audit.json').read_text(encoding='utf-8'))
        self.assertNotIn(run.run_id, audit['argument_quote_refs'])
        self.assertIn(second.run_id, audit['argument_quote_refs'])
        self.assertIn('Historical assessment:', (final / 'audit.md').read_text(encoding='utf-8'))
        self.assertEqual(helper.project.integrity_errors(), [])

    def test_adversarial_contexts_remain_bound_to_historical_source(self):
        helper = self.helper(test_adversarial_review.AdversarialReviewTests)
        document = helper.project._review_document_record()[1]
        session = helper.prepare()
        response = helper.response(session)
        response['result']['context_evidence'][0]['quote'] = 'nglish'
        defended = helper.collect(session, response)
        assessment = helper.assess(defended)
        response2 = helper.response(assessment, 'assessment')
        response2['result']['context_evidence'][0]['quote'] = 'nglish'
        completed = helper.collect(assessment, response2, stage='assessment')
        identity = completed['quote_refs']['defense'][0]
        self.assertEqual(completed['quote_refs']['assessment'], [identity])
        quote = completed['quote_contexts'][identity]
        self.assert_bound(quote, document, response['result']['context_evidence'][0]['block_id'])
        self.assertIn('partial-token', quote['flags'])
        historical_bytes = {p: p.read_bytes() for p in (helper.project.root / 'adversarial-reviews').rglob('*') if p.is_file()}
        helper.helper.followup(revised_text='负责人：项目经理；NEW-ROUND-ONLY English')
        historical = next(s for s in helper.project.adversarial_reviews() if s['session_id'] == session['session_id'])
        self.assertFalse(historical['current'])
        self.assertEqual(historical['quote_contexts'][identity], quote)
        self.assertNotEqual(historical['quote_contexts'][identity]['document_id'], helper.project._review_document_record()[1].document_id)
        self.assertEqual({p: p.read_bytes() for p in historical_bytes}, historical_bytes)

    def test_external_new_findings_have_revised_source_context_and_source_pane(self):
        helper = self.helper(test_resolution_evidence.ResolutionEvidenceTests)
        document = studio._document_from_dict(json.loads((helper.revision_dir / 'document.json').read_text(encoding='utf-8')))
        block = next(b for b in document.blocks if 'English' in b.text)
        finding = helper.helper.finding(document=document, block=block, identity='NEW-FRAGMENT')
        finding['evidence'] = 'nglish'
        helper.payload['new_findings'] = [finding]
        helper.collect()
        status = helper.project.external_recheck_status(helper.revision_id)
        items = status['requests'][0]['items']
        original = next(row for row in items if row['kind'] == 'original')
        new = next(row for row in items if row['kind'] == 'new')
        self.assertEqual(new['primary_quote'], {'block_id': block.block_id, 'quote': 'nglish'})
        self.assertIn(block.block_id, {b['block_id'] for b in status['revised_blocks']})
        for item, bid in ((original, helper.anchor['block_id']), (new, block.block_id)):
            review = status['quote_contexts'][item['quote_refs'][0]]
            self.assert_bound(review, document, bid)
        self.assertIn('partial-token', status['quote_contexts'][new['quote_refs'][0]]['flags'])
        raw_results = {p: p.read_bytes() for p in (helper.revision_dir / 'external-rechecks').rglob('*') if p.is_file()}
        folder = helper.project.export()
        audit = json.loads((folder / 'audit.json').read_text(encoding='utf-8'))
        self.assertEqual(audit['external_recheck']['quote_contexts'], status['quote_contexts'])
        report = (folder / 'audit.md').read_text(encoding='utf-8')
        self.assertIn('Revised evidence', report)
        self.assertIn('nglish', report)
        self.assertEqual({p: p.read_bytes() for p in raw_results}, raw_results)
        self.assertEqual(helper.project.integrity_errors(), [])


if __name__ == '__main__':
    unittest.main()
