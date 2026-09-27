"""Persist a synthetic legacy recheck for browser evidence/reimport checks."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import document_review_studio as studio
from test.test_review_round_protocols import CRITIC, ReviewRoundProtocolTests
from unified_app import serve_unified_app


fixture = ReviewRoundProtocolTests()
fixture.setUp()
project = fixture.project
try:
    blocks = project.document().blocks
    request = fixture.request()
    payload = fixture.response(request)
    payload["findings"] = [fixture.finding(block=blocks[0], identity="F-CHECKED"),
                           fixture.finding(block=blocks[1], identity="F-UNABLE")]
    run = fixture.collect(request, payload)
    for finding in run.findings:
        project.decide_finding(finding.finding_id, "accept", reason="Browser fixture: explicitly accept")
    plan = project.prepare_revision_plan()
    revised_text = '<img src=x onerror="window.__resolutionInjected=true"> 简体中文 Straße 日本語 Русский Latīna · Revised owner: Ada'
    for action in plan["actions"]:
        deleting = action["block_id"] == blocks[1].block_id
        project.set_revision_action_operation(action["action_id"], "delete_block" if deleting else "replace_block",
                                              reason="Browser fixture: preserve first ID, remove second")
        hunk = project.propose_revision_hunk(action["action_id"], "" if deleting else revised_text,
                                             rationale="Browser fixture revision")
        project.decide_revision_hunk(hunk["hunk_id"], "approve", reason="Browser fixture approval")
    revision_dir = project.finalize_revision()
    revision = json.loads((revision_dir / "revision.json").read_text(encoding="utf-8"))
    external = project.external_recheck_status(revision["revision_id"])["requests"][0]
    # A new tracked historical-style result has no source-evidence validation.
    # Existing requests, artifacts, receipts and index entries are never rewritten.
    resolutions = [{"finding_id": finding.finding_id, "state": "still-present",
                    "reason": "Legacy model proposal: 简体中文 Straße 日本語",
                    "evidence": '<img src=x onerror="window.__resolutionInjected=true"> Legacy explanatory text'}
                   for finding in run.findings]
    response = {key: external[key] for key in ("request_id", "prompt_sha256", "revision_id", "revised_sha256", "critic")}
    response.update(resolutions=resolutions, new_findings=[])
    raw = studio.canonical_json(response)
    request_path = revision_dir / "external-recheck-requests" / CRITIC / "request.json"
    result_id = "ER-BROWSER-LEGACY"
    raw_path = revision_dir / "external-rechecks" / CRITIC / (result_id + ".raw.txt")
    studio._write_tracked(project.root, raw_path, raw,
                          parents=[studio._parent_ref(project.root, request_path, role="external-recheck-request")],
                          provenance="model-raw-external-recheck")
    result = {**response, "artifact_type": "external-critic-recheck-result", "schema_version": 2,
              "result_id": result_id, "raw_response_sha256": studio._sha256(raw),
              "declared_model_metadata": {"provider": "fixture", "model": "legacy-rechecker", "import_mode": "manual"},
              "created_at": "2020-01-01T00:00:00+00:00", "lifecycle": "immutable"}
    studio._write_tracked(project.root, raw_path.with_name(result_id + ".json"), studio.canonical_json(result),
                          parents=[studio._parent_ref(project.root, request_path, role="external-recheck-request"),
                                   studio._parent_ref(project.root, raw_path, role="raw-model-response")],
                          provenance="model-parsed-external-recheck")
    assert not project.integrity_errors(), project.integrity_errors()
    server, url = serve_unified_app(data_dir=Path(sys.argv[1]), project_dir=project.root, open_browser=False)
    print(url, flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
finally:
    fixture.doCleanups()
