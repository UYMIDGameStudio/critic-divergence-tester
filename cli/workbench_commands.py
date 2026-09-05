"""Workbench Commands extracted from the public CLI facade."""

from __future__ import annotations

from .support import *  # noqa: F401,F403
from .utils import *  # noqa: F401,F403

def ir_init_command(args: argparse.Namespace) -> int:
    source_path = resolve_manuscript_path(args.manuscript)
    project_dir = (
        Path(args.project_dir)
        if args.project_dir
        else source_path.with_name(source_path.stem + ".argument-workbench")
    )
    paths = initialize_workspace(
        source_path,
        project_dir,
        title=args.title,
    )
    print(f"Argument Workbench project: {paths.root}")
    print(f"Extraction prompt: {paths.prompt}")
    return 0


def ir_ui_command(args: argparse.Namespace) -> int:
    server, url = serve_workbench(
        args.project,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )
    print(f"Argument Workbench UI: {url}")
    print("Local-only session; press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArgument Workbench UI stopped.")
    finally:
        server.server_close()
    return 0


def app_command(args: argparse.Namespace) -> int:
    project_dir = args.project
    if args.manuscript:
        source_path = resolve_manuscript_path(args.manuscript)
        project_dir = Path(args.project) if args.project else source_path.with_name(
            source_path.stem + ".argument-workbench"
        )
        initialize_workspace(source_path, Path(project_dir), title=args.title)
    server, url = serve_product_app(
        data_dir=args.data_dir,
        project_dir=project_dir,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )
    print(f"Argument Workbench: {url}")
    print(f"Local projects: {Path(args.data_dir or default_data_dir()).resolve()}")
    print("Local-only session; press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nArgument Workbench stopped.")
    finally:
        server.server_close()
    return 0


def studio_command(args: argparse.Namespace) -> int:
    """Start the Document Review Studio loopback application."""
    server, url = serve_document_review_studio(
        data_dir=args.data_dir,
        project_dir=args.project,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
    )
    print(f"Document Review Studio: {url}")
    print(f"Local projects: {Path(args.data_dir or default_studio_data_dir()).resolve()}")
    print("Local-only session; press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDocument Review Studio stopped.")
    finally:
        server.server_close()
    return 0


def studio_protocols_command(args: argparse.Namespace) -> int:
    try:
        project = DocumentReviewProject(Path(args.project))
        rows = project.prepare_ai_audits(args.critic or None, provider=args.provider, model=args.model)
    except (OSError, ValueError, ReviewStudioError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    for row in rows:
        print(f"{row['critic']}\t{row['request_id']}\t{row['prompt_sha256']}\t{row['relative_path']}")
    return 0


def studio_import_ai_command(args: argparse.Namespace) -> int:
    try:
        project = DocumentReviewProject(Path(args.project))
        response = Path(args.file).read_bytes()
        run = project.collect_model_audit(args.critic, response, provider=args.provider, model=args.model, request_id=args.request_id, binding_mode=args.binding_mode)
    except (OSError, ValueError, ReviewStudioError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"run_id": run.run_id, "critic": run.critic, "findings": len(run.findings), "model_label": run.model_label}, ensure_ascii=False))
    return 0


def ir_import_version_command(args: argparse.Namespace) -> int:
    source_path = resolve_manuscript_path(args.manuscript)
    paths = import_document_version(
        args.project,
        source_path,
        parent_version=args.parent_version,
    )
    print(f"DocumentVersion: {paths.version_id}")
    print(f"Archived source: {paths.version_dir / 'source' / source_path.name}")
    print(f"Extraction prompt: {paths.prompt}")
    return 0


def ir_diff_versions_command(args: argparse.Namespace) -> int:
    paths, changed = build_structural_diff(
        args.project,
        from_version=args.from_version,
        to_version=args.to_version,
    )
    print(f"Structural diff: {paths.record}")
    print(f"Readable diff: {paths.markdown}")
    print("Structural diff generated." if changed else "Structural diff already current.")
    return 0


def ir_lineage_prepare_command(args: argparse.Namespace) -> int:
    paths, created = prepare_lineage_analysis(
        args.project,
        from_version=args.from_version,
        to_version=args.to_version,
    )
    print(f"Lineage analysis: {paths.record}")
    print(f"Model prompt: {paths.prompt}")
    print("Lineage analysis prepared." if created else "Matching immutable analysis already exists.")
    return 0


def ir_lineage_collect_command(args: argparse.Namespace) -> int:
    if args.paste:
        response_bytes = _read_review_paste_bytes()
        method = "terminal-paste"
        source_name = "pasted-claim-lineage-proposals.json"
    else:
        response_path = Path(args.file).resolve()
        if response_path.is_symlink() or not response_path.is_file():
            raise WorkbenchError("lineage proposal input must be a regular non-symlink file")
        response_bytes = response_path.read_bytes()
        method = "file"
        source_name = response_path.name
    attempt_path, record = collect_lineage_proposals(
        args.project,
        response_bytes,
        from_version=args.from_version,
        to_version=args.to_version,
        analysis_id=args.analysis_id,
        method=method,
        source_name=source_name,
        producer_label=args.producer_label,
    )
    print(f"Lineage proposal attempt: {attempt_path}")
    print(f"Validation status: {record['validation']['status']}")
    for error in record["validation"]["errors"]:
        print(f"  - {error}")
    return 0 if record["validation"]["status"] == "valid" else EXIT_INVALID_WORKFLOW


def ir_lineage_show_command(args: argparse.Namespace) -> int:
    rendered, path = show_lineage(
        args.project,
        from_version=args.from_version,
        to_version=args.to_version,
        analysis_id=args.analysis_id,
    )
    print(rendered, end="" if rendered.endswith("\n") else "\n")
    print(f"Readable lineage: {path}")
    return 0


def ir_lineage_adjudicate_command(args: argparse.Namespace) -> int:
    proposal_ids = lineage_proposal_ids(
        args.project, from_version=args.from_version,
        to_version=args.to_version, analysis_id=args.analysis_id,
    )
    selected = proposal_ids if args.all else list(args.proposal or [])
    if args.all and args.expected_count != len(proposal_ids):
        raise WorkbenchError(
            f"--expected-count {args.expected_count} does not match {len(proposal_ids)} proposals"
        )
    correction = None
    if args.decision == "correct":
        correction = {
            "from_claims": list(args.from_claim or []),
            "to_claims": list(args.to_claim or []),
            "relation": args.relation,
            "semantic_changes": list(args.semantic_change or []),
            "reason": args.lineage_reason,
            "basis_refs": list(args.basis_ref or []),
            "uncertainty": args.uncertainty,
        }
    outputs = append_lineage_decision(
        args.project, proposal_ids=selected, decision=args.decision,
        human_note=args.reason, from_version=args.from_version,
        to_version=args.to_version, analysis_id=args.analysis_id,
        correction=correction,
    )
    for output in outputs:
        print(f"Human lineage decision: {output}")
    return 0


def ir_lineage_history_command(args: argparse.Namespace) -> int:
    print(render_lineage_history(
        args.project, from_version=args.from_version,
        to_version=args.to_version, analysis_id=args.analysis_id,
    ))
    return 0


def ir_resolve_prepare_command(args: argparse.Namespace) -> int:
    paths, created = prepare_resolution(
        args.project, args.finding_id, from_version=args.from_version,
        to_version=args.to_version, lineage_decision_id=args.lineage_decision_id,
    )
    print(f"Finding Resolution: {paths.record}")
    print(f"Original-Lens retest prompt: {paths.root / 'resolution-retest-prompt.md'}")
    print("Resolution retest prepared." if created else "Matching immutable retest already exists.")
    return 0


def ir_resolve_collect_command(args: argparse.Namespace) -> int:
    if args.paste:
        response_bytes = _read_review_paste_bytes(); method = "terminal-paste"; source_name = "pasted-resolution-retest.json"
    else:
        response_path = Path(args.file).resolve()
        if response_path.is_symlink() or not response_path.is_file(): raise WorkbenchError("resolution result input must be a regular file")
        response_bytes = response_path.read_bytes(); method = "file"; source_name = response_path.name
    path, record = collect_resolution_results(args.project, response_bytes, resolution_id=args.resolution_id, method=method, source_name=source_name, producer_label=args.producer_label)
    print(f"Resolution retest attempt: {path}"); print(f"Validation status: {record['validation']['status']}")
    for error in record["validation"]["errors"]: print(f"  - {error}")
    return 0 if record["validation"]["status"] == "valid" else EXIT_INVALID_WORKFLOW


def ir_resolve_decide_command(args: argparse.Namespace) -> int:
    output = append_resolution_decision(args.project, resolution_id=args.resolution_id, decision=args.decision, reason=args.reason, final_status=args.final_status)
    print(f"Human resolution decision: {output}"); return 0


def ir_resolve_show_command(args: argparse.Namespace) -> int:
    print(render_resolution(args.project, args.resolution_id), end=""); return 0


def ir_citations_prepare_command(args: argparse.Namespace) -> int:
    paths, created = prepare_citation_audit(
        args.project, citation_ids=args.citation, version_id=args.version
    )
    print(f"Citation Audit: {paths.audit_id}")
    print(f"Citation audit prompt: {paths.prompt}")
    print("Citation audit prepared." if created else "Matching Citation Audit already exists; reused.")
    return 0


def ir_citations_collect_command(args: argparse.Namespace) -> int:
    if args.paste:
        response_bytes = _read_review_paste_bytes()
        method = "terminal-paste"
        source_name = "pasted-citation-audit.json"
    else:
        response_path = Path(args.file).resolve()
        if response_path.is_symlink() or not response_path.is_file():
            raise WorkbenchError("Citation Audit input must be a regular file")
        response_bytes = response_path.read_bytes()
        method = "file"
        source_name = response_path.name
    path, record = collect_citation_results(
        args.project,
        response_bytes,
        audit_id=args.audit_id,
        version_id=args.version,
        method=method,
        source_name=source_name,
        producer_label=args.producer_label,
    )
    print(f"Citation result attempt: {path}")
    print(f"Validation status: {record['validation']['status']}")
    for error in record["validation"]["errors"]:
        print(f"  - {error}")
    return 0 if record["validation"]["status"] == "valid" else EXIT_INVALID_WORKFLOW


def ir_citations_decide_command(args: argparse.Namespace) -> int:
    final_outcome = None
    if args.decision == "correct":
        missing = [
            option
            for option, value in (
                ("--bibliographic-existence", args.bibliographic_existence),
                ("--exact-source-located", args.exact_source_located),
                ("--content-support", args.content_support),
                ("--context-preserved", args.context_preserved),
            )
            if value is None
        ]
        if missing:
            raise WorkbenchError(
                "decision=correct requires " + ", ".join(missing)
            )
        final_outcome = {
            "bibliographic_existence": args.bibliographic_existence,
            "exact_source_located": args.exact_source_located,
            "content_support": args.content_support,
            "context_preserved": args.context_preserved,
            "uncertainty": args.uncertainty,
        }
    output = append_citation_decision(
        args.project,
        audit_id=args.audit_id,
        version_id=args.version,
        citation_id=args.citation,
        decision=args.decision,
        reason=args.reason,
        final_outcome=final_outcome,
        producer=args.producer_label or "local-user",
    )
    print(f"Human Citation decision: {output}")
    return 0


def ir_citations_show_command(args: argparse.Namespace) -> int:
    print(
        render_citation_audit(
            args.project, audit_id=args.audit_id, version_id=args.version
        ),
        end="",
    )
    return 0


def ir_citations_rebuild_command(args: argparse.Namespace) -> int:
    outputs, changed = rebuild_citation_audits(args.project)
    for output in outputs:
        print(f"Citation provenance: {output}")
    print("Citation provenance rebuilt." if changed else "Citation provenance already current.")
    return 0


def ir_gate_b_init_command(args: argparse.Namespace) -> int:
    paths = initialize_gate_b(args.output, args.projects)
    print(f"Product Gate B evidence: {paths.root}")
    return 0


def ir_gate_b_assess_command(args: argparse.Namespace) -> int:
    output = append_gate_b_assessment(
        args.gate, args.project,
        lineage_correction_minutes=args.lineage_correction_minutes,
        lineage_reasonable=args.lineage_reasonable,
        split_merge_worked=args.split_merge_worked,
        finding_inheritance_correct=args.finding_inheritance_correct,
        resolved_stopped_reappearing=args.resolved_stopped_reappearing,
        unresolved_persisted=args.unresolved_persisted,
        revision_rationale_clarity=args.revision_rationale_clarity,
        notes=args.notes,
    )
    print(f"Gate B assessment: {output}")
    return 0


def ir_gate_b_report_command(args: argparse.Namespace) -> int:
    output, _ = rebuild_gate_b_report(args.gate)
    print(output.read_text(encoding="utf-8"), end="") if args.show else print(f"Gate B report: {output}")
    return 0


def ir_gate_b_decide_command(args: argparse.Namespace) -> int:
    output = append_gate_b_decision(args.gate, args.decision, args.reason)
    print(f"Gate B decision: {output}")
    return 0


def ir_gate_b_verify_command(args: argparse.Namespace) -> int:
    return _ir_print_validation("product-gate-b", verify_gate_b(args.gate))


def _read_ir_paste_bytes() -> bytes:
    print(
        "Paste the model's pure Argument IR JSON. On a new line enter "
        f"{IR_PASTE_END_MARKER} to finish."
    )
    lines: list[str] = []
    total = 0
    while True:
        line = sys.stdin.readline()
        if line == "":
            raise WorkbenchError(
                f"paste ended before {IR_PASTE_END_MARKER}; no artifact was collected"
            )
        normalized = line.rstrip("\r\n")
        if normalized == IR_PASTE_END_MARKER:
            break
        total += len(line.encode("utf-8"))
        if total > DEFAULT_MAX_OUTPUT_BYTES:
            raise WorkbenchError(
                f"pasted Raw IR exceeds {DEFAULT_MAX_OUTPUT_BYTES} bytes"
            )
        lines.append(normalized)
    return ("\n".join(lines) + "\n").encode("utf-8")


def ir_collect_command(args: argparse.Namespace) -> int:
    paths = workspace_paths(args.project)
    if args.paste:
        response_bytes = _read_ir_paste_bytes()
        method = "terminal-paste"
        source_name = "pasted-argument-ir.json"
    else:
        raw_file = Path(args.file)
        if raw_file.is_symlink():
            raise WorkbenchError("Raw IR input must not be a symbolic link")
        response_path = raw_file.resolve()
        if not response_path.is_file():
            raise WorkbenchError(f"Raw IR input file does not exist: {response_path}")
        response_bytes = response_path.read_bytes()
        if len(response_bytes) > DEFAULT_MAX_OUTPUT_BYTES:
            raise WorkbenchError(
                f"Raw IR input exceeds {DEFAULT_MAX_OUTPUT_BYTES} bytes"
            )
        method = "file"
        source_name = response_path.name
    attempt_path, record = collect_raw_attempt(
        paths.root,
        response_bytes,
        method=method,
        source_name=source_name,
        producer_label=args.producer_label,
    )
    status = record["validation"]["status"]
    print(f"Raw IR attempt: {attempt_path}")
    print(f"Validation status: {status}")
    for error in record["validation"]["errors"]:
        print(f"  - {error}")
    active_attempt: Path | None = None
    if status in {"valid", "correctable"}:
        active_attempt, _, _ = selected_attempt(paths)
        if active_attempt != attempt_path:
            print(
                "This attempt was archived but not selected because the project already has "
                f"an inspectable Raw IR: {active_attempt}"
            )
            return 0
    if status == "valid":
        map_path, _ = rebuild_workspace(paths.root)
        print(f"Reviewed IR initialized: {map_path}")
        return 0
    if status == "correctable":
        print("The Raw IR is structurally inspectable; run `ir inspect` to correct it.")
        return 0
    print("The attempt was preserved but cannot be inspected; collect a new attempt.")
    return EXIT_INVALID_WORKFLOW


def ir_inspect_command(args: argparse.Namespace) -> int:
    if not args.view_only:
        isatty = getattr(sys.stdin, "isatty", None)
        if isatty is not None and not isatty():
            raise WorkbenchError(
                "interactive inspection requires a terminal; use --view-only for non-interactive output"
            )
    return run_inspector(args.project, view_only=args.view_only)


def ir_rebuild_command(args: argparse.Namespace) -> int:
    map_path, changed = rebuild_workspace(args.project)
    review_outputs, reviews_changed = rebuild_reviews(args.project)
    perspective_outputs, perspectives_changed = rebuild_perspective_reviews(
        args.project
    )
    diff_outputs, diffs_changed = rebuild_structural_diffs(args.project)
    lineage_outputs, lineages_changed = rebuild_lineage_analyses(args.project)
    resolution_outputs, resolutions_changed = rebuild_resolutions(args.project)
    citation_outputs, citations_changed = rebuild_citation_audits(args.project)
    triage_outputs, triage_changed = rebuild_status_triages(args.project)
    adjudication_outputs, adjudications_changed = rebuild_adjudication_cache(
        args.project
    )
    print(f"Argument map: {map_path}")
    for output in review_outputs:
        print(f"Claim review: {output}")
    for output in perspective_outputs:
        print(f"Perspective review: {output}")
    for output in diff_outputs:
        print(f"Structural diff: {output}")
    for output in lineage_outputs:
        print(f"Claim lineage: {output}")
    for output in resolution_outputs:
        print(f"Finding resolution: {output}")
    for output in citation_outputs:
        print(f"Citation provenance: {output}")
    for output in triage_outputs:
        print(f"Status triage: {output}")
    for output in adjudication_outputs:
        print(f"Revision plan: {output}")
    print(
        "Derived artifacts rebuilt."
        if changed
        or reviews_changed
        or perspectives_changed
        or diffs_changed
        or lineages_changed
        or resolutions_changed
        or citations_changed
        or triage_changed
        or adjudications_changed
        else "Derived artifacts already current."
    )
    return 0


def ir_verify_project_command(args: argparse.Namespace) -> int:
    errors = verify_project_versions(args.project)
    return _ir_print_validation("argument-workbench-project", errors)


def ir_review_prepare_command(args: argparse.Namespace) -> int:
    paths, created = prepare_rule_review(
        args.project,
        args.rules,
        depth=args.depth,
        review_scope=args.scope,
        claim_ids=args.claim,
    )
    print(f"Rule Review: {paths.review_id}")
    print(f"Review prompt: {paths.prompt}")
    print(f"Check plan: {paths.plan}")
    print("Review prepared." if created else "Matching review already exists; reused.")
    return 0


def _read_review_paste_bytes() -> bytes:
    print(
        "Paste the model's pure Review Lens results JSON. On a new line enter "
        f"{IR_PASTE_END_MARKER} to finish."
    )
    lines: list[str] = []
    total = 0
    while True:
        line = sys.stdin.readline()
        if line == "":
            raise WorkbenchError(
                f"paste ended before {IR_PASTE_END_MARKER}; no review result was collected"
            )
        normalized = line.rstrip("\r\n")
        if normalized == IR_PASTE_END_MARKER:
            break
        total += len(line.encode("utf-8"))
        if total > DEFAULT_MAX_OUTPUT_BYTES:
            raise WorkbenchError(
                f"pasted review result exceeds {DEFAULT_MAX_OUTPUT_BYTES} bytes"
            )
        lines.append(normalized)
    return ("\n".join(lines) + "\n").encode("utf-8")


def ir_review_collect_command(args: argparse.Namespace) -> int:
    if args.paste:
        response_bytes = _read_review_paste_bytes()
        method = "terminal-paste"
        source_name = "pasted-check-results.json"
    else:
        raw_file = Path(args.file)
        if raw_file.is_symlink():
            raise WorkbenchError("review result input must not be a symbolic link")
        response_path = raw_file.resolve()
        if not response_path.is_file():
            raise WorkbenchError(f"review result file does not exist: {response_path}")
        response_bytes = response_path.read_bytes()
        if len(response_bytes) > DEFAULT_MAX_OUTPUT_BYTES:
            raise WorkbenchError(
                f"review result input exceeds {DEFAULT_MAX_OUTPUT_BYTES} bytes"
            )
        method = "file"
        source_name = response_path.name
    attempt_path, record = collect_review_results(
        args.project,
        response_bytes,
        review_id=args.review_id,
        method=method,
        source_name=source_name,
        producer_label=args.producer_label,
    )
    status = record["validation"]["status"]
    print(f"Review result attempt: {attempt_path}")
    print(f"Validation status: {status}")
    for error in record["validation"]["errors"]:
        print(f"  - {error}")
    if status != "valid":
        print("The attempt was preserved; collect a corrected complete/partial result.")
        return EXIT_INVALID_WORKFLOW
    review_text, view_path = show_claim_review(
        args.project,
        review_id=str(record["review_id"]),
        claim_id=None,
    )
    actionable = review_text.count("### FAIL ") + review_text.count("### UNCERTAIN ")
    print(f"Claim review: {view_path}")
    print(f"Open Findings: {actionable}")
    return 0


def ir_review_show_command(args: argparse.Namespace) -> int:
    rendered, view_path = show_claim_review(
        args.project,
        review_id=args.review_id,
        claim_id=args.claim,
    )
    print(rendered, end="" if rendered.endswith("\n") else "\n")
    print(f"Full claim review: {view_path}")
    return 0


def ir_review_prepare_perspective_command(args: argparse.Namespace) -> int:
    paths, created = prepare_perspective_review(
        args.project,
        lens_id=args.lens,
        review_scope=args.scope,
        claim_ids=args.claim,
    )
    print(f"Perspective Review: {paths.review_id}")
    print(f"Lens: {args.lens}")
    print(f"Review prompt: {paths.prompt}")
    print(f"Perspective plan: {paths.plan}")
    print("Review prepared." if created else "Matching review already exists; reused.")
    return 0


def ir_review_collect_perspective_command(args: argparse.Namespace) -> int:
    if args.paste:
        response_bytes = _read_review_paste_bytes()
        method = "terminal-paste"
        source_name = "pasted-perspective-results.json"
    else:
        raw_file = Path(args.file)
        if raw_file.is_symlink():
            raise WorkbenchError("Perspective result input must not be a symbolic link")
        response_path = raw_file.resolve()
        if not response_path.is_file():
            raise WorkbenchError(
                f"Perspective result file does not exist: {response_path}"
            )
        response_bytes = response_path.read_bytes()
        if len(response_bytes) > DEFAULT_MAX_OUTPUT_BYTES:
            raise WorkbenchError(
                f"Perspective result input exceeds {DEFAULT_MAX_OUTPUT_BYTES} bytes"
            )
        method = "file"
        source_name = response_path.name
    attempt_path, record = collect_perspective_results(
        args.project,
        response_bytes,
        review_id=args.review_id,
        method=method,
        source_name=source_name,
        producer_label=args.producer_label,
    )
    status = record["validation"]["status"]
    print(f"Perspective result attempt: {attempt_path}")
    print(f"Validation status: {status}")
    for error in record["validation"]["errors"]:
        print(f"  - {error}")
    if status != "valid":
        print("The attempt was preserved; collect a corrected result.")
        return EXIT_INVALID_WORKFLOW
    rendered, view_path = show_perspective_review(
        args.project,
        review_id=str(record["review_id"]),
        claim_id=None,
    )
    actionable = rendered.count(" — FAIL ") + rendered.count(" — UNCERTAIN ")
    print(f"Perspective review: {view_path}")
    print(f"Open Findings: {actionable}")
    return 0


def ir_review_show_perspective_command(args: argparse.Namespace) -> int:
    rendered, view_path = show_perspective_review(
        args.project,
        review_id=args.review_id,
        claim_id=args.claim,
    )
    print(rendered, end="" if rendered.endswith("\n") else "\n")
    print(f"Full Perspective review: {view_path}")
    return 0


def ir_review_show_claim_lenses_command(args: argparse.Namespace) -> int:
    rendered = render_claim_lenses(args.project, args.claim)
    print(rendered, end="" if rendered.endswith("\n") else "\n")
    return 0


def ir_review_triage_command(args: argparse.Namespace) -> int:
    mutation_values = (args.task, args.decision, args.action, args.note)
    if any(value is not None for value in mutation_values):
        if not all(value is not None for value in mutation_values):
            raise WorkbenchError(
                "status triage mutation requires --task, --decision, --action, and --note"
            )
        output = append_status_triage(
            args.project,
            review_id=args.review_id,
            task_id=args.task,
            decision=args.decision,
            action=args.action,
            note=args.note,
            producer=args.producer_label or "local-user",
        )
        print(f"Status triage event: {output}")
    review, attempt_id, items = triage_items_for_review(
        args.project, review_id=args.review_id
    )
    print(render_status_triage(items), end="")
    print(f"Review status queue: {review.review_id}/{attempt_id}")
    return 0


def ir_adjudicate_command(args: argparse.Namespace) -> int:
    batch_fields = (
        args.batch_decision,
        args.reason,
        args.confirm_count,
        args.action,
        args.producer_label,
    )
    batch_requested = any(value is not None for value in batch_fields)
    if batch_requested:
        if (
            args.batch_decision is None
            or args.reason is None
            or args.confirm_count is None
        ):
            raise WorkbenchError(
                "batch adjudication requires --batch-decision, --reason, and --confirm-count"
            )
        if args.claim is None:
            raise WorkbenchError("batch adjudication requires exactly one --claim")
        if args.review_all or args.view_only or args.summary_only or args.group_by_claim:
            raise WorkbenchError(
                "batch adjudication cannot be combined with --review-all, --view-only, "
                "--summary-only, or --group-by-claim"
            )
        actions: list[tuple[str, str]] = []
        for raw_action in args.action or []:
            action_type, separator, text = raw_action.partition(":")
            if (
                not separator
                or action_type not in REVISION_ACTION_TYPES
                or not text.strip()
            ):
                allowed = ", ".join(REVISION_ACTION_TYPES)
                raise WorkbenchError(
                    "--action must use ACTION_TYPE:TEXT with one of: " + allowed
                )
            actions.append((action_type, text.strip()))
        created = append_claim_bundle_decisions(
            args.project,
            claim=args.claim,
            decision=args.batch_decision,
            reason=args.reason,
            actions=actions,
            confirm_count=args.confirm_count,
            review_id=args.review_id,
            verdict=args.verdict,
            check_id=args.check_id,
            producer=args.producer_label or "local-user",
        )
        action_count = sum(len(action_paths) for _, action_paths in created)
        print(
            f"Recorded {len(created)} independent human adjudications "
            f"and {action_count} RevisionAction artifacts."
        )
        print(
            claim_bundle_status(
                args.project,
                review_id=args.review_id,
                verdict=args.verdict,
                claim=args.claim,
                check_id=args.check_id,
            ),
            end="",
        )
        return 0
    if args.group_by_claim:
        if args.review_all:
            raise WorkbenchError(
                "--group-by-claim shows only open Findings; omit --review-all"
            )
        print(
            claim_bundle_status(
                args.project,
                review_id=args.review_id,
                verdict=args.verdict,
                claim=args.claim,
                check_id=args.check_id,
            ),
            end="",
        )
        return 0
    if not args.view_only and not args.summary_only:
        isatty = getattr(sys.stdin, "isatty", None)
        if isatty is not None and not isatty():
            raise WorkbenchError(
                "interactive Workbench adjudication requires a terminal; use --view-only for non-interactive output"
            )
    return run_workbench_adjudicator(
        args.project,
        review_id=args.review_id,
        review_all=args.review_all,
        view_only=args.view_only,
        verdict=args.verdict,
        claim=args.claim,
        check_id=args.check_id,
        summary_only=args.summary_only,
    )


def ir_revision_plan_command(args: argparse.Namespace) -> int:
    plan_path, changed = rebuild_workbench_revision_plan(args.project)
    print(f"Revision plan: {plan_path}")
    print("Revision plan rebuilt." if changed else "Revision plan already current.")
    if args.show:
        print(plan_path.read_text(encoding="utf-8"), end="")
    return 0


def ir_gate_a_init_command(args: argparse.Namespace) -> int:
    paths = initialize_gate(args.output, args.projects)
    print(f"Product Gate A corpus: {paths.corpus}")
    print(f"Evidence report: {paths.report_markdown}")
    print("Only hashes and local workspace locators were stored; manuscript bytes were not copied.")
    return 0


def ir_gate_a_readiness_command(args: argparse.Namespace) -> int:
    print(render_gate_readiness(gate_readiness(args.projects)), end="")
    return 0


def ir_gate_a_baseline_command(args: argparse.Namespace) -> int:
    paths = collect_direct_review_baseline(
        args.project,
        prompt_file=args.prompt_file,
        response_file=args.response_file,
        model_label=args.model_label,
        model_provider=args.model_provider,
        model_id=args.model_id,
        interaction_mode=args.interaction_mode,
        prior_context=args.prior_context,
        manuscript_delivery=args.manuscript_delivery,
        full_manuscript_confirmed=args.full_manuscript_confirmed,
        started_at=args.started_at,
        completed_at=args.completed_at,
        producer_label=args.producer_label,
    )
    print(f"Direct-review baseline: {paths.record}")
    print("Exact prompt/response bytes and elapsed time were preserved.")
    print("No comparison or Gate decision was made.")
    return 0


def ir_gate_a_prepare_baseline_command(args: argparse.Namespace) -> int:
    output, digest = prepare_direct_review_prompt(args.project, args.output)
    print(f"Direct-review prompt: {output}")
    print(f"SHA-256: {digest}")
    print("The bound manuscript bytes are embedded verbatim.")
    return 0


def ir_gate_a_session_start_command(args: argparse.Namespace) -> int:
    paths = start_work_session(
        args.project,
        activity=args.activity,
        note=args.note,
        producer=args.producer_label,
    )
    print(f"Gate A work session started: {paths.session_id}")
    print(f"Start artifact: {paths.start}")
    return 0


def ir_gate_a_session_finish_command(args: argparse.Namespace) -> int:
    paths = finish_work_session(
        args.project,
        args.session,
        producer=args.producer_label,
    )
    print(f"Gate A work session completed: {paths.session_id}")
    print(f"Session artifact: {paths.record}")
    return 0


def ir_gate_a_session_abandon_command(args: argparse.Namespace) -> int:
    paths = abandon_work_session(
        args.project,
        args.session,
        reason=args.reason,
        producer=args.producer_label,
    )
    print(f"Gate A work session abandoned: {paths.session_id}")
    print(f"Abandonment artifact: {paths.record}")
    print("This interval will not count as completed Gate A work.")
    return 0


def ir_gate_a_session_list_command(args: argparse.Namespace) -> int:
    print(render_work_sessions(list_work_sessions(args.project)), end="")
    return 0


def ir_gate_a_assess_command(args: argparse.Namespace) -> int:
    metrics = {key: getattr(args, key) for key in GATE_A_METRIC_KEYS}
    if args.correction_minutes is not None:
        metrics["correction_minutes"] = args.correction_minutes
    output = append_gate_a_assessment(
        args.gate,
        args.project,
        comparison_to_direct_chat=args.comparison,
        correction_burden=args.burden,
        metrics=metrics,
        regression_anchors=args.anchor,
        actual_revision_notes=args.actual_revision_notes,
        notes=args.notes,
    )
    print(f"Human Gate A assessment: {output}")
    return 0


def ir_gate_a_report_command(args: argparse.Namespace) -> int:
    report, changed = rebuild_gate_report(args.gate)
    print(f"Product Gate A report: {report}")
    print("Report rebuilt." if changed else "Report already current.")
    if args.show:
        print(report.read_text(encoding="utf-8"), end="")
    return 0


def ir_gate_a_decide_command(args: argparse.Namespace) -> int:
    output = append_gate_decision(args.gate, args.decision, args.reason)
    print(f"Human Gate A decision: {output}")
    return 0


def ir_gate_a_verify_command(args: argparse.Namespace) -> int:
    return _ir_print_validation("product-gate-a", verify_gate(args.gate))


def ir_prepare_command(args: argparse.Namespace) -> int:
    source_path = resolve_manuscript_path(args.manuscript)
    manuscript, source_bytes = read_manuscript_utf8(source_path)
    prompt = build_ir_extraction_prompt(
        manuscript,
        source_name=source_path.name,
        source_sha256=sha256_bytes(source_bytes),
    )
    output = (
        Path(args.output)
        if args.output
        else source_path.with_name(source_path.stem + ".argument-ir-prompt.md")
    )
    prompt_bytes = prompt.encode("utf-8")
    _ir_write_outputs(((output, prompt_bytes),), inputs=(source_path,))
    print(f"Argument IR extraction prompt: {output.resolve()}")
    print(f"Source SHA-256: {sha256_bytes(source_bytes)}")
    return 0


def ir_validate_command(args: argparse.Namespace) -> int:
    source_path = resolve_manuscript_path(args.manuscript)
    _, source_bytes = read_manuscript_utf8(source_path)
    _, value, _ = _ir_read_json(Path(args.argument_ir), "argument IR")
    errors = validate_argument_ir(
        value,
        source_bytes=source_bytes,
        source_name=source_path.name,
    )
    return _ir_print_validation("argument-ir", errors)


def ir_plan_command(args: argparse.Namespace) -> int:
    source_path = resolve_manuscript_path(args.manuscript)
    _, source_bytes = read_manuscript_utf8(source_path)
    ir_path, ir_value, ir_bytes = _ir_read_json(
        Path(args.argument_ir), "argument IR"
    )
    library_path, library_value, library_bytes = _ir_read_json(
        Path(args.rules), "check library"
    )
    errors = validate_argument_ir(
        ir_value,
        source_bytes=source_bytes,
        source_name=source_path.name,
    )
    errors.extend(validate_check_library(library_value))
    if errors:
        return _ir_print_validation("argument-ir-plan-inputs", errors)
    normalized_ir = canonicalize_argument_ir(
        ir_value,
        source_bytes=source_bytes,
        source_name=source_path.name,
    )
    plan = build_check_plan(
        normalized_ir,
        library_value,
        ir_sha256=sha256_bytes(ir_bytes),
        library_sha256=sha256_bytes(library_bytes),
        depth=args.depth,
        review_scope=args.scope,
        claim_ids=args.claim,
    )
    plan_errors = validate_check_plan(plan)
    if plan_errors:
        return _ir_print_validation("argument-check-plan", plan_errors)
    plan_bytes = _ir_json_bytes(plan)
    plan_sha256 = sha256_bytes(plan_bytes)
    prompt_bytes = render_check_prompt(plan, plan_sha256=plan_sha256).encode("utf-8")
    output = (
        Path(args.output)
        if args.output
        else ir_path.with_name("argument-check-plan.json")
    )
    prompt_output = (
        Path(args.prompt_output)
        if args.prompt_output
        else ir_path.with_name("argument-check-prompt.md")
    )
    _ir_write_outputs(
        ((output, plan_bytes), (prompt_output, prompt_bytes)),
        inputs=(source_path, ir_path, library_path),
    )
    print(f"Check plan: {output.resolve()}")
    print(f"Execution prompt: {prompt_output.resolve()}")
    print(f"Plan SHA-256: {plan_sha256}")
    print(f"Tasks: {len(plan['tasks'])}")
    return 0


def ir_validate_results_command(args: argparse.Namespace) -> int:
    _, plan, plan_bytes = _ir_read_json(Path(args.check_plan), "check plan")
    _, results, _ = _ir_read_json(Path(args.results), "check results")
    _, library, library_bytes = _ir_read_json(Path(args.rules), "check library")
    plan_errors = validate_check_plan_against_library(
        plan,
        library,
        library_sha256=sha256_bytes(library_bytes),
    )
    if plan_errors:
        return _ir_print_validation("argument-check-plan", plan_errors)
    errors = validate_check_results(
        results,
        plan,
        plan_sha256=sha256_bytes(plan_bytes),
    )
    return _ir_print_validation("argument-check-results", errors)


def ir_findings_command(args: argparse.Namespace) -> int:
    plan_path, plan, plan_bytes = _ir_read_json(Path(args.check_plan), "check plan")
    results_path, results, results_bytes = _ir_read_json(
        Path(args.results), "check results"
    )
    _, library, library_bytes = _ir_read_json(Path(args.rules), "check library")
    plan_errors = validate_check_plan_against_library(
        plan,
        library,
        library_sha256=sha256_bytes(library_bytes),
    )
    if plan_errors:
        return _ir_print_validation("argument-check-plan", plan_errors)
    plan_sha256 = sha256_bytes(plan_bytes)
    errors = validate_check_results(results, plan, plan_sha256=plan_sha256)
    if errors:
        return _ir_print_validation("argument-check-results", errors)
    findings = build_argument_findings(
        plan,
        results,
        plan_sha256=plan_sha256,
        results_sha256=sha256_bytes(results_bytes),
    )
    findings_errors = validate_argument_findings(findings)
    if findings_errors:
        return _ir_print_validation("argument-findings", findings_errors)
    findings_bytes = _ir_json_bytes(findings)
    output = (
        Path(args.output)
        if args.output
        else results_path.with_name("argument-findings.json")
    )
    _ir_write_outputs(
        ((output, findings_bytes),), inputs=(plan_path, results_path)
    )
    print(f"Findings: {output.resolve()}")
    print(f"Actionable findings: {len(findings['findings'])}")
    return 0

__all__ = [name for name in globals() if not name.startswith("__")]
