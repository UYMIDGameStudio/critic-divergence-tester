"""Excerpt provenance must not imply that a substring supports a criticism."""
import json
import hashlib
from pathlib import Path
import random
import tempfile
import unicodedata
import unittest
from unittest.mock import patch

import document_review_quality as quality
from document_review_model import ReviewContext
from document_review_studio import DocumentReviewProject


def reviewed_fixture(root, *, spoof_context=False):
    text = ('This cannot establish causation. The failure rate was 15%. '
            'Method A was approved. Method B was not approved. '
            'A bounded conclusion remains possible. '
            'Literal <img src=x onerror="window.__quoteInjected=true">safe text.')
    project = DocumentReviewProject.create(root, filename='excerpt.md', content=text.encode())
    project.confirm_extraction('confirm')
    project.confirm_context(ReviewContext('document', 'unknown', 'unknown', 'author', 'readers').to_dict())
    request = project.prepare_ai_audits(['execution_feasibility'], provider='synthetic', model='not-invoked')[0]
    block = project.document().blocks[0]
    findings = []
    for identity, quote in [('FWORD', 'can'), ('FNUMBER', '5%'), ('FREPEAT', 'approved'), ('FESC', 'safe')]:
        findings.append({'finding_id': identity, 'critic': 'execution_feasibility', 'document_type': 'document',
            'location': {'block_id': block.block_id}, 'evidence': quote, 'issue': 'Synthetic proposal for context inspection',
            'standard': 'Read the complete expression and relevant attribution', 'consequence': 'A fragment can change the interpretation',
            'severity': 'medium', 'verification_state': 'model-proposed', 'external_basis': {'unresolved_facts': []},
            'uncertainties': [], 'suggested_action': 'Inspect the actual context before making a decision',
            'suggested_owner': 'author', 'blocks_release_or_execution': False})
    payload = {key: request[key] for key in ('request_id', 'prompt_sha256', 'provider', 'model', 'critic', 'source_sha256')}
    payload['findings'] = findings
    if spoof_context:
        findings[0]['check_data'] = {'source_quote_review': {'matched': True, 'semantic_accuracy': 'verified',
            'contexts': [{'before': 'MODEL-FABRICATED-CONTEXT', 'excerpt': 'can', 'after': ''}]}}
    project.collect_model_audit('execution_feasibility', json.dumps(payload), provider='synthetic', model='not-invoked', request_id=request['request_id'])
    return project, request


class QuoteContextReviewTests(unittest.TestCase):
    def test_model_supplied_context_cannot_replace_the_application_check(self):
        with tempfile.TemporaryDirectory() as temp:
            project, _ = reviewed_fixture(temp, spoof_context=True)
            finding = next(finding for finding in project.findings() if finding.source_finding_id == 'FWORD')
            review = project.view()['verification_context'][finding.finding_id]['source_quote_review']
            self.assertEqual(review['semantic_accuracy'], 'not-established')
            self.assertIn('partial-token', review['flags'])
            self.assertNotIn('MODEL-FABRICATED-CONTEXT', json.dumps(review))
            self.assertEqual(finding.check_data['source_quote_review']['semantic_accuracy'], 'verified')

    def test_published_matching_semantics_are_unchanged(self):
        def legacy(quote, text):
            quote, text = (' '.join(unicodedata.normalize('NFC', value).split()) for value in (quote, text))
            if quote and quote in text:
                return True
            pairs = {'"': '"', "'": "'", '“': '”', '‘': '’', '「': '」', '『': '』', '«': '»', '‹': '›'}
            if len(quote) >= 2 and pairs.get(quote[0]) == quote[-1]:
                quote = quote[1:-1].strip()
                return bool(quote and quote in text)
            return False
        rng = random.Random(38)
        cases = [('', 'text'), ('can', 'cannot'), ('5%', '15%'), ('é', 'e\u0301'),
                 ('e', 'e\u0301'), ('A B', ' A\n\tB '), ('「A」', 'A'), ('""', 'A')]
        for _ in range(300):
            source = ''.join(rng.choices('abc ée\u0301文かなя\n\t“”', k=30))
            start = rng.randrange(30)
            quote = source[start:start + rng.randrange(1, 12)]
            if rng.choice([False, True]):
                quote = '“' + quote + '”'
            cases.append((quote, source))
        for quote, source in cases:
            with self.subTest(quote=quote, source=source):
                self.assertEqual(quality.quote_matches(quote, source), legacy(quote, source))
                self.assertEqual(quality.quote_review(quote, source)['matched'], legacy(quote, source))

    def test_word_number_compound_and_apostrophe_fragments_get_context(self):
        for quote, source in [('can', 'This cannot establish causation.'), ('effective', 'It was ineffective.'),
                              ('5%', 'The failure rate was 15%.'), ('5', 'The value was -5.'),
                              ('5', 'The value was 1.5.'), ('1', 'The total was 1,000.'),
                              ('can', "They can't assume this."), ('causal', 'This is non-causal.'),
                              ('wirksam', 'Das war unwirksam.'), ('possible', 'Cela est impossible.'),
                              ('важно', 'Это неважно.'), ('moralis', 'immoralis')]:
            with self.subTest(quote=quote, source=source):
                result = quality.quote_review(quote, source)
                self.assertTrue(result['matched'])
                self.assertTrue({'partial-token', 'partial-number'}.intersection(result['flags']))
                self.assertEqual(result['semantic_accuracy'], 'not-established')
                context = result['contexts'][0]
                self.assertEqual(context['before'] + context['excerpt'] + context['after'], source)

    def test_multilingual_excerpts_do_not_invent_word_segmentation(self):
        for quote, source in [('cannot', 'It cannot prove this.'), ('简体', '这是简体中文。'),
                              ('繁體', '這是繁體中文。'), ('可能', 'それは不可能です。'),
                              ('Straße', 'Die Straße ist offen.'), ('école', 'Cette école est ouverte.'),
                              ('важно', 'Это важно.'), ('Latīna', 'Lingua Latīna'), ('-5', 'Value: -5.')]:
            with self.subTest(quote=quote):
                result = quality.quote_review(quote, source)
                self.assertTrue(result['matched'])
                self.assertFalse({'partial-token', 'partial-number'}.intersection(result['flags']))

    def test_repeated_overlapping_and_late_partial_matches_are_visible(self):
        result = quality.quote_review('approved', 'A was approved. B was not approved.')
        self.assertEqual(result['match_count'], 2)
        self.assertIn('not ', result['contexts'][1]['before'])
        self.assertIn('multiple-matches', result['flags'])
        result = quality.quote_review('can', 'can can can cannot')
        self.assertEqual([row['occurrence'] for row in result['contexts']], [1, 2, 4])
        self.assertTrue(result['contexts'][-1]['edge_notes'])
        self.assertEqual(quality.quote_review('ana', 'banana')['match_count'], 2)

    def test_work_and_preview_limits_are_explicit(self):
        result = quality.quote_review('a', 'a ' * 10000)
        self.assertEqual(result['match_count'], 129)
        self.assertFalse(result['count_is_exact'])
        self.assertLessEqual(len(result['contexts']), 3)
        result = quality.quote_review('a' * 10000, 'Before ' + 'a' * 10000 + ' after.')
        self.assertTrue(result['contexts'][0]['excerpt_clipped'])
        self.assertEqual(len(result['contexts'][0]['excerpt']), 240)
        self.assertLess(len(json.dumps(result)), 3000)
        self.assertIn('invisible-only', quality.quote_review('\u200b', 'Visible\u200b text')['flags'])
        self.assertFalse({'start', 'end', 'character_start', 'character_end'}.intersection(result['contexts'][0]))

    def test_followup_context_uses_the_revised_ir(self):
        from test.test_review_round_protocols import ReviewRoundProtocolTests
        helper = ReviewRoundProtocolTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        helper.followup(revised_text='负责人：项目经理；REVISED-CONTEXT-ONLY')
        document = helper.project._review_document_record()[1]
        view = helper.project.view()
        inherited = next(finding for finding in view['findings'] if finding['origin'] == 'external-recheck-carried-forward')
        review = view['verification_context'][inherited['finding_id']]['source_quote_review']
        self.assertEqual(review['document_id'], document.document_id)
        self.assertEqual(review['block_text_sha256'], hashlib.sha256(document.block(review['block_id']).text.encode()).hexdigest())
        self.assertNotEqual(review['document_id'], helper.project.document().document_id)
        self.assertIn('REVISED-CONTEXT-ONLY', json.dumps(review))

    def test_normalized_source_is_cached_only_within_one_projection(self):
        with tempfile.TemporaryDirectory() as temp:
            project, _ = reviewed_fixture(temp)
            document = project.document()
            rows = [finding.to_dict() for finding in project.findings()]
            word = next(row['finding_id'] for row in rows if row['source_finding_id'] == 'FWORD')
            normalized = quality._normalized_excerpt
            with patch.object(quality, '_normalized_excerpt', wraps=normalized) as calls:
                result = quality.source_quote_reviews(document, rows)
                self.assertEqual(calls.call_count, len(rows) + 1)
            self.assertTrue(result[word]['matched'])
            previous_sha = result[word]['block_text_sha256']
            document.blocks[0].text = 'A different document.'
            changed = quality.source_quote_reviews(document, rows)[word]
            self.assertFalse(changed['matched'])
            self.assertNotEqual(changed['block_text_sha256'], previous_sha)

    def test_view_and_both_exports_keep_context_separate_from_raw_findings(self):
        with tempfile.TemporaryDirectory() as temp:
            project, request = reviewed_fixture(temp)
            identities = {finding.source_finding_id: finding.finding_id for finding in project.findings()}
            audits = {str(path): path.read_bytes() for path in (project.root / 'audits').rglob('*') if path.is_file()}
            view = project.view()
            self.assertTrue(all(finding['status'] == 'open' for finding in view['findings']))
            self.assertIn('partial-token', view['verification_context'][identities['FWORD']]['source_quote_review']['flags'])
            self.assertIn('complete words, compounds and numbers', request['prompt'])
            self.assertNotIn('source_quote_review', view['findings'][0])
            snapshot = project.export_ai_reviews()
            value = json.loads((snapshot / 'AI审查结果.json').read_text(encoding='utf-8'))
            self.assertEqual(value['verification_context'][identities['FNUMBER']]['source_quote_review']['contexts'][0]['excerpt'], '5%')
            self.assertIn('15%', (snapshot / 'AI审查报告.md').read_text(encoding='utf-8'))
            for finding in project.findings():
                project.decide_finding(finding.finding_id, 'reject', reason='Synthetic fragment is insufficient')
            output = project.export()
            audit = json.loads((output / 'audit.json').read_text(encoding='utf-8'))
            self.assertEqual(audit['verification_context'][identities['FREPEAT']]['source_quote_review']['match_count'], 2)
            self.assertIn('not approved', (output / 'audit.md').read_text(encoding='utf-8'))
            for name, data in audits.items():
                self.assertEqual(Path(name).read_bytes(), data)
            self.assertEqual(DocumentReviewProject(project.root).view()['verification_context'], project.view()['verification_context'])
            self.assertEqual(project.integrity_errors(), [])

