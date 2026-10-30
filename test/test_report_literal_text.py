"""Reports treat external prose as text; raw responses and manuscripts stay exact."""
import difflib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import document_review_studio as studio

from document_review_adversarial import ENVELOPE_FIELDS, response_example
from document_review_model import ReviewContext
from document_review_quality import markdown_text
from document_review_studio import DocumentReviewProject, _difference_report
from test.test_argument_composition import synthetic_assessment


LANGUAGES = 'English 简体中文 繁體中文 Straße français 日本語 Русский Latīna'
ATTACK = ('<img src="https://report.invalid/image" onerror="window.__reportInjected=true">\n\n'
          '# FORGED APPROVAL\n\n![remote](https://report.invalid/image)\n\n'
          '[execute](javascript:window.__reportInjected=true)\n\n'
          '</code><svg onload="window.__reportInjected=true"></svg>\n\n'
          '```\n<script>window.__reportInjected=true</script>\n```\n\n'
          '~~~\n<iframe src="https://report.invalid/frame"></iframe>\n~~~\n\n'
          '[target]: https://report.invalid/target\n\n[go][target]\n\n'
          'FORGED UNDERLINE\n===\n\n- forged list\n\n1. forged ordered list\n\n'
          '&lt;script&gt; &amp; &#x3c; \\ `literal` **emphasis** | cell |\n\n' + LANGUAGES)
META = '<img src=x onerror="window.__reportInjected=true"> ![meta](https://report.invalid/meta)'


def export_fixture(root):
    """Only owned synthetic projects. Used by Python and the reference-renderer browser check."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    source = ('Article for export checks.\n\n' + ATTACK).encode()
    project = DocumentReviewProject.create(root / 'library', filename='report`[name].txt', content=source)
    project.confirm_extraction('confirm')
    project.confirm_context(ReviewContext('document', 'unknown', 'unknown', 'author', 'readers').to_dict())
    document = project.document()
    block = next(b for b in document.blocks if '<img' in b.text)
    request = project.prepare_ai_audits(['execution_feasibility'], provider=META, model=META)[0]
    finding = {'finding_id': 'REPORT', 'critic': request['critic'], 'document_type': 'document',
        'location': {'block_id': block.block_id}, 'evidence': block.text,
        **{key: ATTACK for key in ('issue', 'standard', 'consequence', 'suggested_action')},
        'severity': 'medium', 'verification_state': 'model-proposed',
        'external_basis': {'unresolved_facts': [ATTACK]}, 'uncertainties': [],
        'suggested_owner': 'author', 'blocks_release_or_execution': False,
        'check_data': {'close_reading': {**{key: ATTACK for key in
            ('author_position', 'strongest_defense', 'why_defense_fails', 'repair_test')},
            'context_evidence': [{'block_id': block.block_id, 'quote': block.text, 'role': 'qualification'}]}}}
    payload = {key: request[key] for key in ('request_id', 'prompt_sha256', 'provider', 'model', 'critic', 'source_sha256')}
    payload['findings'] = [finding]
    run = project.collect_model_audit(request['critic'], json.dumps(payload), provider=META, model=META, request_id=request['request_id'])
    finding_id = run.findings[0].finding_id
    session = project.prepare_adversarial_review(finding_id, provider=META, model=META)
    for stage, fields in (('defense', ('author_position', 'strongest_defense', 'limitations')),
                          ('assessment', ('reasons', 'remaining_issue', 'minimal_repair', 'repair_test'))):
        if stage == 'assessment':
            session = project.prepare_adversarial_assessment(session['session_id'], provider=META, model=META)
        task = session['requests'][-1]
        result = response_example(stage)
        result.update({key: ATTACK for key in fields})
        result['context_evidence'][0].update(block_id=block.block_id, quote=block.text)
        response = {**{key: task[key] for key in ENVELOPE_FIELDS}, 'result': result}
        session = project.collect_adversarial_response(session['session_id'], json.dumps(response), request_id=task['request_id'])
    records = {p: p.read_bytes() for folder in ('audits', 'adversarial-reviews')
               for p in (project.root / folder).rglob('*') if p.is_file()}
    ai = project.export_ai_reviews()
    snapshot = json.loads((ai / 'AI审查结果.json').read_text(encoding='utf-8'))
    assert snapshot['findings'][0]['issue'] == ATTACK
    assert snapshot['report_rendering_version'] == 1
    assert snapshot['adversarial_reviews'][0]['defense']['strongest_defense'] == ATTACK
    project.decide_finding(finding_id, 'correct', reason=ATTACK, corrected_action=ATTACK)
    stable_id = studio.stable_id
    def old_bridge_id(prefix, *parts):
        return stable_id(prefix, *parts[1:]) if prefix == 'BRG' else stable_id(prefix, *parts)
    # Materialize a historical-style bridge once, without rewriting receipts.
    with patch.object(studio, 'stable_id', side_effect=old_bridge_id), patch.object(studio, 'markdown_text', side_effect=str), patch.object(studio, 'REPORT_RENDERING_VERSION', 0):
        old_bridge = project.prepare_revision_bridge()
    old_bridge_bytes = old_bridge.read_bytes()
    bridge = project.prepare_revision_bridge()
    assert bridge != old_bridge and old_bridge.read_bytes() == old_bridge_bytes
    assert project.prepare_revision_bridge() == bridge
    assert json.loads((bridge.parent / 'bridge.json').read_text(encoding='utf-8'))['report_rendering_version'] == 1
    action = project.prepare_revision_plan()['actions'][0]
    project.set_revision_action_operation(action['action_id'], 'replace_block', reason=ATTACK)
    hunk = project.propose_revision_hunk(action['action_id'], 'Revised safe text.\n\n' + ATTACK, rationale=ATTACK)
    project.decide_revision_hunk(hunk['hunk_id'], 'approve', reason=ATTACK)
    def old_difference(before, after):
        return '# V1/V2 difference report\n\n```diff\n' + ''.join(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True))) + '```\n'
    # A pre-upgrade Revision remains immutable; fresh formal export must render
    # a safe difference from the bound manuscript instead of copying its report.
    with patch.object(studio, '_difference_report', side_effect=old_difference):
        revision_dir = project.finalize_revision()
    old_difference_bytes = (revision_dir / '修改说明.md').read_bytes()
    revision = json.loads((revision_dir / 'revision.json').read_text(encoding='utf-8'))
    request = project.external_recheck_requests(revision['revision_id'])[0]
    revised = json.loads((revision_dir / 'document.json').read_text(encoding='utf-8'))
    anchor = next(b for b in revised['blocks'] if b['text'] == 'Revised safe text.')
    payload = {key: request[key] for key in ('request_id', 'prompt_sha256', 'critic', 'revision_id', 'revised_sha256')}
    payload.update(resolutions=[{'finding_id': finding_id, 'state': 'still-present', 'reason': ATTACK,
        'evidence': ATTACK, 'source_evidence': [{'block_id': anchor['block_id'], 'quote': anchor['text']}]}], new_findings=[])
    result = project.collect_external_recheck(revision['revision_id'], request['critic'], json.dumps(payload), provider=META, model=META)
    project.decide_external_resolution(revision['revision_id'], result['result_id'], finding_id, 'unresolved', reason=ATTACK)
    final = project.export()
    audit = json.loads((final / 'audit.json').read_text(encoding='utf-8'))
    assert audit['report_rendering_version'] == 1
    assert audit['external_recheck']['requests'][0]['items'][0]['human_decision']['reason'] == ATTACK
    assert {p: p.read_bytes() for p in records} == records
    assert (project.root / project.document().source.relative_path).read_bytes() == source
    assert ATTACK in (revision_dir / '修改稿.md').read_text(encoding='utf-8')
    assert (revision_dir / '修改说明.md').read_bytes() == old_difference_bytes
    assert (final / '修改说明.md').read_bytes() != old_difference_bytes
    assert not project.integrity_errors(), project.integrity_errors()
    paths = {'ai-review': ai / 'AI审查报告.md', 'formal-review': final / 'audit.md',
             'revision-bridge': bridge, 'revision-difference': final / '修改说明.md',
             'revision-risks': revision_dir / '未解决风险.md', 'composed-risks': final / '未解决风险.md',
             'word-export-info': final / 'Word导出说明.md'}
    academic = DocumentReviewProject.create(root / 'library', filename='academic.txt', content=(
        'Bounded comparison\n\nThe question concerns two interpretations.\n\n'
        'First distinguish their shared assumptions.\n\nThen compare the same passage.\n\n'
        'The conclusion preserves what both interpretations establish.').encode())
    academic.confirm_extraction('confirm')
    academic.confirm_context(ReviewContext('academic article', 'unknown', 'unknown', 'author', 'researchers',
        review_profile='academic', discipline='humanities', research_type='theoretical').to_dict())
    task = academic.prepare_ai_audits(['academic_argument'], provider='fixture', model='not-invoked')[0]
    assessment = synthetic_assessment(academic)
    for key in ('question', 'intended_contribution', 'argument_route'):
        assessment['review_frame'][key] = ATTACK
    assessment['review_frame'].update(priorities=[ATTACK], scope_limits=[ATTACK])
    for axis in assessment['dimensions']:
        axis.update(title=META, why_relevant=ATTACK, description=ATTACK, assessment=ATTACK,
                    strengths=[ATTACK], limitations=[ATTACK], recommendation=ATTACK)
    response = {key: task[key] for key in ('request_id', 'prompt_sha256', 'provider', 'model', 'critic', 'source_sha256')}
    response.update(findings=[], zero_finding_basis=[ATTACK], argument_assessment=assessment)
    academic.collect_model_audit(task['critic'], json.dumps(response), provider='fixture', model='not-invoked', request_id=task['request_id'])
    academic_ai = academic.export_ai_reviews()
    academic_final = academic.export()
    assert json.loads((academic_final / 'audit.json').read_text(encoding='utf-8'))['audit_runs'][0]['argument_assessment'] == assessment
    assert not academic.integrity_errors(), academic.integrity_errors()
    paths.update({'academic-ai': academic_ai / 'AI审查报告.md', 'academic-formal': academic_final / 'audit.md'})
    output = root / 'reports'
    output.mkdir(exist_ok=True)
    for name, p in paths.items():
        (output / (name + '.md')).write_bytes(p.read_bytes())
    (output / 'literal-probe.md').write_text(markdown_text(ATTACK), encoding='utf-8')
    return {'reports': {name: str(output / (name + '.md')) for name in [*paths, 'literal-probe']},
            'literal': ATTACK, 'languages': LANGUAGES, 'source_name': document.source.original_name,
            'source_and_model_records_unchanged': True, 'historical_reports_preserved': True}


class ReportLiteralTextTests(unittest.TestCase):
    def test_markup_and_line_starts_are_neutralized_without_rewriting_ordinary_ids(self):
        self.assertEqual(markdown_text('F-123 1.5% Straße 日本語'), 'F-123 1.5% Straße 日本語')
        self.assertNotIn('<script>', markdown_text(ATTACK))
        self.assertNotIn('\n# FORGED', markdown_text(ATTACK))
        self.assertIn('\\!\\[remote\\]', markdown_text(ATTACK))
        self.assertEqual(markdown_text('1. text'), '1\\. text')
        self.assertEqual(markdown_text('2) text'), '2\\) text')
        self.assertEqual(markdown_text('Verified (model proposal)'), 'Verified (model proposal)')
        self.assertEqual(markdown_text('a\r\nb\rc'), 'a\nb\nc')

    def test_difference_cannot_escape_literal_block_and_preserves_missing_newline_markers(self):
        report = _difference_report('old', '```\n<script>window.__reportInjected=true</script>')
        self.assertIn('    -old\n    \\ No newline at end of file', report)
        self.assertIn('    +<script>', report)
        self.assertNotIn('\n<script>', report)
        self.assertIn('Native Word Track Changes are not claimed.', report)
        before = 'old\n```\n<img src=x onerror="window.__reportInjected=true">\n```\ntail\n'
        report = _difference_report(before, before.replace('old', 'new', 1))
        self.assertIn('     ```\n     <img', report)

    def test_real_exports_preserve_raw_material_and_escape_every_report_family(self):
        with tempfile.TemporaryDirectory() as temp:
            result = export_fixture(temp)
            self.assertTrue(result['source_and_model_records_unchanged'])
            self.assertTrue(result['historical_reports_preserved'])
            self.assertEqual(len(result['reports']), 10)
            for name, path in result['reports'].items():
                report = Path(path).read_text(encoding='utf-8')
                self.assertNotIn('\n# FORGED APPROVAL', report, name)
                for line in report.splitlines():
                    if '<img ' in line or '<script>' in line:
                        self.assertTrue(line.startswith('    '), (name, 'raw markup must be in a literal block'))


if __name__ == '__main__':
    unittest.main()
