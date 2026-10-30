"""Lossless source reuse, full current context and immutable request compatibility."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from document_review_model import ReviewContext, canonical_json
from document_review_source_projection import (HISTORY_MARKER, SOURCE_MARKER, REVISED_MARKER,
    project_historical_prompt, validate_historical_projection)
from document_review_studio import DocumentReviewProject, ReviewStudioError


LANGUAGES = 'English 简体中文 繁體中文 Straße français 日本語 Русский Latīna e\u0301'


def source_section(text, marker):
    return json.JSONDecoder().raw_decode(text.split(marker, 1)[1])[0]


def reconstruct(projected, current):
    payload = source_section(projected, SOURCE_MARKER)
    if isinstance(payload, list):
        return payload
    lookup = {block['block_id']: block for block in current}
    return [lookup[item['same_as_current']] if 'same_as_current' in item else item['historical_block']
            for item in payload['entries']]


def long_review_fixture(root, *, paragraphs=90):
    """One real revision; all model output is synthetic and never quality-scored."""
    body = '# Original scope\n\n' + '\n\n'.join(
        f'Passage {i}: {LANGUAGES}. ' + 'The interpretation remains bounded by its stated assumptions. ' * 5
        for i in range(paragraphs))
    project = DocumentReviewProject.create(root, filename='article.md', content=body.encode())
    project.confirm_extraction('confirm')
    project.confirm_context(ReviewContext('document', 'unknown', 'unknown', 'author', 'readers').to_dict())
    request = project.prepare_ai_audits(['execution_feasibility'], provider='synthetic', model='not-invoked')[0]
    first = project.document().blocks[0]
    finding = {'finding_id': 'SCOPE', 'critic': request['critic'], 'document_type': 'document',
        'location': {'block_id': first.block_id}, 'evidence': first.text,
        'issue': 'Synthetic scoped issue', 'standard': 'Synthetic criterion', 'consequence': 'Synthetic consequence',
        'severity': 'low', 'verification_state': 'model-proposed', 'external_basis': {'unresolved_facts': []},
        'uncertainties': [], 'suggested_action': 'Clarify the scope', 'suggested_owner': 'author',
        'blocks_release_or_execution': False}
    payload = {key: request[key] for key in ('request_id', 'prompt_sha256', 'provider', 'model', 'critic', 'source_sha256')}
    payload['findings'] = [finding]
    imported = project.collect_model_audit(request['critic'], json.dumps(payload), provider='synthetic',
        model='not-invoked', request_id=request['request_id'])
    finding_id = imported.findings[0].finding_id
    project.decide_finding(finding_id, 'accept', reason='Synthetic human approval')
    action = project.prepare_revision_plan()['actions'][0]
    project.set_revision_action_operation(action['action_id'], 'replace_block', reason='Synthetic operation')
    hunk = project.propose_revision_hunk(action['action_id'], '# Revised scope', rationale='Synthetic clarification')
    project.decide_revision_hunk(hunk['hunk_id'], 'approve', reason='Synthetic approval')
    revision_dir = project.finalize_revision()
    revision = json.loads((revision_dir / 'revision.json').read_text(encoding='utf-8'))
    return project, request, revision


class SourceProjectionTests(unittest.TestCase):
    def blocks(self):
        return [{'block_id': f'B-{i}', 'kind': 'paragraph', 'text': f'{i} {LANGUAGES} ' * 12,
                 'location': {'page': i + 1, 'paragraph': i}, 'attrs': {'measure': 0.5, 'flag': True},
                 'children': []} for i in range(12)]

    def original(self, blocks):
        return ('ORIGINAL POLICY: do not replace my standards.\n' + SOURCE_MARKER
                + json.dumps(blocks, ensure_ascii=False, indent=2)
                + '\n```\nEXTRA POLICY: retain qualifications and attribution.\n').encode()

    def envelope(self, original, current):
        projected, metadata = project_historical_prompt(original, current)
        prompt = (HISTORY_MARKER + projected + '\n\n## Original Findings\n[]\n'
                  + REVISED_MARKER + json.dumps(current, ensure_ascii=False) + '\n```\n').encode()
        return {'schema_version': 4, 'historical_prompt_projection': metadata}, prompt, projected

    def test_all_blocks_and_policy_survive_changed_deleted_added_and_reordered_current_blocks(self):
        old = self.blocks()
        current = copy.deepcopy(old)
        current[0]['text'] = 'Changed claim.'
        current.pop(1)
        current.reverse()
        current.append({'block_id': 'GENERATED', 'kind': 'paragraph', 'text': 'New passage.'})
        original = self.original(old)
        projected, metadata = project_historical_prompt(original, current)
        self.assertEqual(metadata['mode'], 'lossless-current-block-references')
        self.assertEqual(metadata['reused_block_count'], 10)
        self.assertEqual(metadata['literal_block_count'], 2)
        self.assertEqual(canonical_json(reconstruct(projected, current)), canonical_json(old))
        self.assertTrue(projected.startswith(original.decode().split(SOURCE_MARKER)[0] + SOURCE_MARKER))
        self.assertTrue(projected.endswith('\n```\nEXTRA POLICY: retain qualifications and attribution.\n'))
        self.assertEqual(metadata['saved_bytes'], len(original) - len(projected.encode()))
        self.assertEqual(metadata['original_prompt_file_sha256'], hashlib.sha256(original).hexdigest())

    def test_entire_json_identity_is_required_including_types_and_metadata(self):
        old = self.blocks()
        changes = [('kind', 'heading'), ('text', 'Other words'), ('location', {'page': 999}),
                   ('attrs', {'measure': 0.5, 'flag': 1}), ('children', ['B-11']), ('block_id', 'NEW-ID')]
        changes.append(('text', old[0]['text'].replace('e\u0301', 'é')))
        for key, value in changes:
            current = copy.deepcopy(old)
            current[0][key] = value
            with self.subTest(key=key):
                projected, _ = project_historical_prompt(self.original(old), current)
                first = source_section(projected, SOURCE_MARKER)['entries'][0]
                self.assertEqual(first, {'historical_block': old[0]})
                self.assertEqual(canonical_json(reconstruct(projected, current)), canonical_json(old))

    def test_unknown_ambiguous_or_noncanonical_old_json_stays_verbatim(self):
        blocks = self.blocks()
        original = self.original(blocks)
        cases = [original.replace(SOURCE_MARKER.encode(), b'## Legacy document\n'),
                 original + SOURCE_MARKER.encode(), self.original(blocks + [blocks[0]]),
                 original.replace(b'"measure": 0.5', b'"measure": 0.500000000000000001', 1),
                 original.replace(b'"page": 1', b'"page": -0', 1),
                 original.replace(b'"page": 1', b'"page": 1, "page": 2', 1),
                 original.replace(b'"measure": 0.5', b'"measure": NaN', 1)]
        for source in cases:
            projected, metadata = project_historical_prompt(source, blocks)
            self.assertEqual(projected.encode(), source)
            self.assertEqual(metadata['mode'], 'verbatim')
            self.assertEqual(metadata['saved_bytes'], 0)
        changed = [{**b, 'text': 'Changed ' + b['text']} for b in blocks]
        self.assertEqual(project_historical_prompt(original, changed)[0].encode(), original)
        small = [{'block_id': 'B', 'kind': 'paragraph', 'text': 'x'}]
        self.assertEqual(project_historical_prompt(self.original(small), small)[0].encode(), self.original(small))

    def test_current_full_source_and_projected_history_are_checked_not_just_a_claimed_hash(self):
        old = self.blocks()
        original = self.original(old)
        request, prompt, projected = self.envelope(original, old)
        self.assertEqual(validate_historical_projection(request, prompt, original, old), [])
        variants = [prompt.replace(b'ORIGINAL POLICY', b'ALTERED POLICY'),
                    prompt.replace(b'"same_as_current":"B-0"', b'"same_as_current":"B-1"'),
                    prompt[:prompt.rindex(REVISED_MARKER.encode())] + REVISED_MARKER.encode() + b'[]\n```\n']
        for bad in variants:
            self.assertTrue(validate_historical_projection(request, bad, original, old))
        for change in ({'version': True}, {'version': 2},
                {'projected_prompt_bytes': float(request['historical_prompt_projection']['projected_prompt_bytes'])}):
            bad = copy.deepcopy(request)
            bad['historical_prompt_projection'].update(change)
            self.assertTrue(validate_historical_projection(bad, prompt, original, old))
        self.assertTrue(validate_historical_projection({'schema_version': 4}, prompt, original, old))
        for version in (1, 2, 3):
            self.assertEqual(validate_historical_projection({'schema_version': version}, original, original, old), [])
        self.assertTrue(validate_historical_projection({'schema_version': 5}, original, original, old))

    def test_real_long_article_recheck_reconstructs_old_source_without_losing_current_context(self):
        with tempfile.TemporaryDirectory() as temp:
            project, initial, revision = long_review_fixture(temp)
            historical_files = {p: p.read_bytes() for folder in ('ai-requests', 'audits')
                                for p in (project.root / folder).rglob('*') if p.is_file()}
            request = project.external_recheck_requests(revision['revision_id'])[0]
            metadata = request['historical_prompt_projection']
            self.assertEqual(request['schema_version'], 4)
            self.assertEqual(metadata['mode'], 'lossless-current-block-references')
            self.assertGreater(metadata['reused_block_count'], 80)
            self.assertGreater(metadata['saved_bytes'], 20000)
            current = source_section(request['prompt'], REVISED_MARKER)
            original = source_section(initial['prompt'], SOURCE_MARKER)
            historical = request['prompt'].split(HISTORY_MARKER, 1)[1]
            self.assertEqual(canonical_json(reconstruct(historical, current)), canonical_json(original))
            directory = project.root / 'revisions' / revision['revision_id']
            self.assertEqual(current, json.loads((directory / 'document.json').read_text(encoding='utf-8'))['blocks'])
            self.assertEqual(request['revised_document_sha256'], hashlib.sha256((directory / 'document.json').read_bytes()).hexdigest())
            self.assertEqual({p: p.read_bytes() for p in historical_files}, historical_files)
            self.assertEqual(project.integrity_errors(), [])

    def test_next_revision_uses_initial_policy_and_current_reference_targets(self):
        from test.test_review_round_protocols import ReviewRoundProtocolTests
        helper = ReviewRoundProtocolTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        initial, _, _ = helper.followup(revised_text='First revised scope')
        project = helper.project
        for finding in project.findings():
            project.decide_finding(finding.finding_id, 'accept', reason='Synthetic next-round decision')
        action = project.prepare_revision_plan()['actions'][0]
        project.set_revision_action_operation(action['action_id'], 'replace_block', reason='Synthetic operation')
        hunk = project.propose_revision_hunk(action['action_id'], 'Second revised scope', rationale='Synthetic next repair')
        project.decide_revision_hunk(hunk['hunk_id'], 'approve', reason='Synthetic approval')
        directory = project.finalize_revision()
        revision = json.loads((directory / 'revision.json').read_text(encoding='utf-8'))
        request = project.external_recheck_requests(revision['revision_id'])[0]
        self.assertEqual(request['original_request_id'], initial['request_id'])
        current = source_section(request['prompt'], REVISED_MARKER)
        self.assertIn('Second revised scope', {b['text'] for b in current})
        historical = request['prompt'].split(HISTORY_MARKER, 1)[1]
        self.assertEqual(canonical_json(reconstruct(historical, current)), canonical_json(source_section(initial['prompt'], SOURCE_MARKER)))
        self.assertEqual(project.integrity_errors(), [])

    def test_new_request_drift_fails_before_import_with_valid_integrity_receipts(self):
        from test.test_resolution_evidence import ResolutionEvidenceTests
        helper = ResolutionEvidenceTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        def metadata_version(request, prompt):
            request['historical_prompt_projection']['version'] = 99
            return request, prompt
        def missing_metadata(request, prompt):
            request.pop('historical_prompt_projection')
            return request, prompt
        def wrong_ir(request, prompt):
            request['revised_document_sha256'] = '0' * 64
            return request, prompt
        def policy_drift(request, prompt):
            return request, prompt.replace('You are exactly one independent critic.', 'Altered historical policy.', 1)
        def incomplete_current(request, prompt):
            before, section = prompt.rsplit(REVISED_MARKER, 1)
            _, length = json.JSONDecoder().raw_decode(section)
            return request, before + REVISED_MARKER + '[]' + section[length:]
        for transform in (metadata_version, missing_metadata, wrong_ir, policy_drift, incomplete_current):
            with self.subTest(case=transform.__name__):
                def materialize(request, prompt):
                    request, prompt = transform(request, prompt)
                    request['prompt_file_sha256'] = hashlib.sha256(prompt.encode()).hexdigest()
                    return request, prompt
                project, _ = helper.request_fixture(materialize)
                with self.assertRaisesRegex(ReviewStudioError, '新旧稿件引用'):
                    project.external_recheck_requests(helper.revision_id)
                helper.assert_rejected_without_writes(helper.payload, project=project)
                self.assertEqual(project.integrity_errors(), [])

    def test_verbatim_fallback_still_checks_current_quotes_in_a_real_import(self):
        with tempfile.TemporaryDirectory() as temp:
            project, _, revision = long_review_fixture(temp, paragraphs=0)
            request = project.external_recheck_requests(revision['revision_id'])[0]
            self.assertEqual(request['historical_prompt_projection']['mode'], 'verbatim')
            self.assertEqual(request['historical_prompt_projection']['saved_bytes'], 0)
            block = source_section(request['prompt'], REVISED_MARKER)[0]
            payload = {key: request[key] for key in ('request_id', 'prompt_sha256', 'critic', 'revision_id', 'revised_sha256')}
            payload.update(resolutions=[{'finding_id': request['original_finding_ids'][0], 'state': 'still-present',
                'reason': 'Synthetic explanation', 'evidence': 'Synthetic evidence explanation',
                'source_evidence': [{'block_id': block['block_id'], 'quote': 'Original scope'}]}], new_findings=[])
            with self.assertRaises(ReviewStudioError):
                project.collect_external_recheck(revision['revision_id'], request['critic'], json.dumps(payload), provider='synthetic', model='not-invoked')
            self.assertEqual(project._external_recheck_results(revision['revision_id']), [])
            payload['resolutions'][0]['source_evidence'][0]['quote'] = block['text']
            result = project.collect_external_recheck(revision['revision_id'], request['critic'], json.dumps(payload), provider='synthetic', model='not-invoked')
            self.assertEqual(result['resolutions'][0]['evidence_validation'], 'checked-revised-excerpts')
            self.assertEqual(project.integrity_errors(), [])

    def test_saved_v3_full_snapshot_replays_without_new_projection_metadata(self):
        from test.test_resolution_evidence import ResolutionEvidenceTests
        helper = ResolutionEvidenceTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        original = (helper.project.root / helper.request['original_prompt_relative_path']).read_text(encoding='utf-8')
        def legacy(request, prompt):
            current = json.loads((helper.revision_dir / 'document.json').read_text(encoding='utf-8'))['blocks']
            projected, _ = project_historical_prompt(original.encode(), current)
            prompt = prompt.replace(HISTORY_MARKER + projected,
                '## Exact original prompt snapshot\n' + original, 1)
            request['schema_version'] = 3
            request.pop('historical_prompt_projection')
            request.pop('revised_document_sha256')
            request['prompt_file_sha256'] = hashlib.sha256(prompt.encode()).hexdigest()
            return request, prompt
        project, request = helper.request_fixture(legacy)
        restored = project.external_recheck_requests(helper.revision_id)[0]
        self.assertEqual(restored['schema_version'], 3)
        self.assertNotIn('historical_prompt_projection', restored)
        self.assertIn(original, restored['prompt'])
        result = helper.collect(project=project)
        self.assertEqual(result['resolutions'][0]['evidence_validation'], 'checked-revised-excerpts')
        self.assertEqual(project.integrity_errors(), [])


if __name__ == '__main__':
    unittest.main()
