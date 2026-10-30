"""Owned manuscripts and genuine persisted reviews for source-reader browser QA."""
import json

from document_review_model import ReviewContext
from document_review_studio import DocumentReviewProject
from test.test_argument_composition import synthetic_assessment

LANGUAGES = 'English 简体中文 繁體中文 Straße français 日本語 Русский Latīna'


def article(root):
    body = ('# Reader scope\n\nA bounded comparison. ' + LANGUAGES + '\n\n### Shared point\n\n'
        + '\n\n'.join(f'Body-position-{i:02d}. ' + LANGUAGES + '. ' + 'A complete paragraph remains available for inspection. ' * 18 for i in range(60))
        + '\n\n## Shared point\n\nThe same label does not mean the same source location.\n\n'
        + '# <img src=x onerror="window.__readerInjected=true">\n\nEND-READER-CONTENT.')
    project = DocumentReviewProject.create(root, filename='reader.md', content=body.encode())
    project.confirm_extraction('confirm')
    project.confirm_context(ReviewContext('academic article', 'unknown', 'unknown', 'author', 'readers',
        review_profile='academic', discipline='humanities', research_type='theoretical').to_dict())
    request = project.prepare_ai_audits(['academic_argument'], provider='synthetic', model='not-invoked')[0]
    response = {key: request[key] for key in ('request_id', 'prompt_sha256', 'provider', 'model', 'critic', 'source_sha256')}
    response.update(findings=[], zero_finding_basis=['Synthetic browser fixture, not a model-quality result.'],
        argument_assessment=synthetic_assessment(project))
    project.collect_model_audit(request['critic'], json.dumps(response), provider='synthetic',
        model='not-invoked', request_id=request['request_id'])
    return project


def pages(root):
    from test.test_office_formats import pptx_entries, zipped
    project = DocumentReviewProject.create(root, filename='source-pages.pptx', content=zipped(pptx_entries()))
    project.confirm_extraction('continue_with_warning')
    project.confirm_context(ReviewContext('document', 'unknown', 'unknown', 'author', 'readers').to_dict())
    return project


def followup(root):
    from test.test_source_projection import long_review_fixture, source_section, REVISED_MARKER
    from studio_ui_state import document_scope, save_ui_draft
    project, _, revision = long_review_fixture(root, paragraphs=8)
    save_ui_draft(project, {'project_directory': project.root.name, 'scope': document_scope(project),
        'revision': 0, 'fields': {'workspace-search': 'OLD-DOCUMENT-ONLY-QUERY',
            'source-reader-open': True, 'source-reader-scroll': '2000'}, 'scroll': 0})
    request = project.external_recheck_requests(revision['revision_id'])[0]
    block = source_section(request['prompt'], REVISED_MARKER)[0]
    payload = {key: request[key] for key in ('request_id', 'prompt_sha256', 'critic', 'revision_id', 'revised_sha256')}
    payload.update(resolutions=[{'finding_id': request['original_finding_ids'][0], 'state': 'still-present',
        'reason': 'Synthetic recheck explanation', 'evidence': 'Synthetic explanation',
        'source_evidence': [{'block_id': block['block_id'], 'quote': block['text']}]}], new_findings=[])
    result = project.collect_external_recheck(revision['revision_id'], request['critic'], json.dumps(payload),
        provider='synthetic', model='not-invoked')
    project.decide_external_resolution(revision['revision_id'], result['result_id'], request['original_finding_ids'][0],
        'unresolved', reason='Synthetic human followup decision')
    project.start_followup_round(revision['revision_id'])
    return project
