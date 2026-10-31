"""Start an empty, isolated library for real browser regression tests."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from unified_app import serve_unified_app

fixture = None
project_dir = None
if '--word-hyperlink-edits' in sys.argv[2:]:
    from document_review_studio import DocumentReviewProject
    from studio_selftest import _word_hyperlink_edit_fixture
    project_dir=DocumentReviewProject.create(Path(sys.argv[1]),filename='linked.docx',content=_word_hyperlink_edit_fixture()).root
if '--html-hyperlinks' in sys.argv[2:]:
    from document_review_studio import DocumentReviewProject
    from studio_selftest import _html_hyperlink_fixture
    project_dir = DocumentReviewProject.create(Path(sys.argv[1]), filename='academic-links.html', content=_html_hyperlink_fixture()).root
if '--docx-hyperlinks' in sys.argv[2:]:
    from document_review_studio import DocumentReviewProject
    from studio_selftest import _docx_hyperlink_fixture
    project_dir = DocumentReviewProject.create(Path(sys.argv[1]), filename='academic-links.docx', content=_docx_hyperlink_fixture()).root
if '--ir-budget' in sys.argv[2:]:
    from document_review_studio import DocumentReviewProject
    from studio_selftest import _ir_budget_fixture
    project_dir = DocumentReviewProject.create(Path(sys.argv[1]), filename='expanded-notes.docx', content=_ir_budget_fixture()).root
if '--docx-notes' in sys.argv[2:]:
    from document_review_studio import DocumentReviewProject
    from studio_selftest import _docx_notes_fixture
    project_dir = DocumentReviewProject.create(Path(sys.argv[1]), filename='academic-notes.docx', content=_docx_notes_fixture()).root
for flag, mode in (('--source-reader', 'article'), ('--reader-pages', 'pages'), ('--reader-followup', 'followup')):
    if flag in sys.argv[2:]:
        from test import source_reader_fixtures
        project_dir = getattr(source_reader_fixtures, mode)(Path(sys.argv[1])).root
if "--source-projection" in sys.argv[2:]:
    from test.test_source_projection import long_review_fixture
    project_dir = long_review_fixture(Path(sys.argv[1]))[0].root
if "--quote-context" in sys.argv[2:]:
    from test.test_quote_context_review import reviewed_fixture
    project_dir = reviewed_fixture(Path(sys.argv[1]))[0].root
if "--empty-body" in sys.argv[2:]:
    from document_review_studio import DocumentReviewProject
    project_dir = DocumentReviewProject.create(Path(sys.argv[1]), filename="empty.csv", content=b',\n,\n').root
if '--empty-history' in sys.argv[2:]:
    from unittest.mock import patch
    from document_review_studio import DocumentReviewProject
    from document_review_model import ReviewContext
    # Reproduce a formerly permitted empty review with valid historical receipts.
    historical = DocumentReviewProject.create(Path(sys.argv[1]), filename='blank.txt', content=b' \n')
    with patch('document_review_studio.has_reviewable_text', return_value=True):
        historical.confirm_extraction('confirm')
        historical.confirm_context(ReviewContext('document', 'unknown', 'unknown', 'author', 'readers').to_dict())
        historical.run_local_prechecks(['expression_ambiguity'])
        historical.prepare_ai_audits(['expression_ambiguity'], provider='fixture', model='not-invoked')
        historical.export()
    project_dir = historical.root
if "--docx-containers" in sys.argv[2:]:
    from document_review_studio import DocumentReviewProject
    from studio_selftest import _docx_container_fixture
    project_dir = DocumentReviewProject.create(Path(sys.argv[1]), filename="containers.docx", content=_docx_container_fixture(special=True)).root
if "--table-grid" in sys.argv[2:]:
    from document_review_studio import DocumentReviewProject
    from studio_selftest import _table_grid_fixture
    project_dir = DocumentReviewProject.create(Path(sys.argv[1]), filename="table-grid.docx", content=_table_grid_fixture()).root
if "--xml-limits" in sys.argv[2:]:
    from document_review_studio import DocumentReviewProject
    from document_review_ingest import IngestionLimits
    from studio_selftest import _docx_visibility_fixture
    project_dir = DocumentReviewProject.create(Path(sys.argv[1]), filename="structure.docx",
        content=_docx_visibility_fixture(), limits=IngestionLimits(max_xml_elements=1)).root
if "--docx-visibility" in sys.argv[2:]:
    from document_review_studio import DocumentReviewProject
    from studio_selftest import _docx_visibility_fixture
    project_dir = DocumentReviewProject.create(Path(sys.argv[1]), filename="visibility.docx", content=_docx_visibility_fixture()).root
if "--readonly-recovery" in sys.argv[2:]:
    from test.test_review_round_protocols import ReviewRoundProtocolTests
    fixture = ReviewRoundProtocolTests()
    fixture.setUp()
    request = fixture.request()
    folder = fixture.project.root / "ai-requests" / request["request_id"]
    (folder / "request.json").write_bytes(b'{"private":"RECOVERY-MUST-NOT-RENDER"')
    (folder / "prompt.md").write_text('RECOVERY-MUST-NOT-RENDER <img src=x onerror="window.__recoveryInjected=true">', encoding="utf-8")
    project_dir = fixture.project.root
if "--argument-composition" in sys.argv[2:]:
    from test.test_argument_composition import ArgumentCompositionTests
    fixture = ArgumentCompositionTests()
    fixture.setUp()
    request = fixture.request()
    payload = fixture.payload(request)
    payload['argument_assessment']['review_frame']['evidence'][0]['quote'] = 'pretations'
    payload['argument_assessment']['dimensions'][1]['evidence'][0]['quote'] = 'pretations'
    payload["argument_assessment"]["dimensions"][3]["assessment"] = '<img src=x onerror="window.__compositionInjected=true"> Straße 日本語 Русский Latīna'
    import copy
    extra = copy.deepcopy(payload["argument_assessment"]["dimensions"][0])
    extra.update(id="problem-framing", title="Problem framing", why_relevant="This synthetic article compares two interpretations.")
    payload["argument_assessment"]["dimensions"].append(extra)
    fixture.collect(request, payload)
    project_dir = fixture.project.root
if "--close-reading" in sys.argv[2:]:
    from test.test_close_reading_quality import CloseReadingQualityTests
    fixture = CloseReadingQualityTests()
    fixture.setUp()
    request = fixture.helper.request()
    finding = fixture.helper.finding()
    detail = fixture.detail()
    english = next(block for block in fixture.project.document().blocks if 'English' in block.text)
    detail['context_evidence'].append({'block_id': english.block_id, 'quote': 'nglish', 'role': 'qualification'})
    detail["strongest_defense"] = '<img src=x onerror="window.__injected=true"> Straße 日本語 Русский Latīna'
    finding["check_data"] = {"close_reading": detail}
    fixture.helper.collect(request, fixture.helper.response(request, finding))
    project_dir = fixture.project.root
server, url = serve_unified_app(data_dir=Path(sys.argv[1]), project_dir=project_dir, open_browser=False)
print(url, flush=True)
try:
    server.serve_forever(poll_interval=0.1)
finally:
    server.server_close()
    if fixture is not None:
        fixture.doCleanups()
