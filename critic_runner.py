"""Public CLI entry point and compatibility facade."""

from cli.support import *  # noqa: F401,F403
from cli.scoring_commands import *  # noqa: F401,F403
from cli.legacy_commands import *  # noqa: F401,F403
from cli.utils import *  # noqa: F401,F403
from cli.workbench_commands import *  # noqa: F401,F403
from cli.validation import *  # noqa: F401,F403
from cli.quickstart import *  # noqa: F401,F403

from cli import support as _support
from cli import core as _core
from cli import run as _run_commands
from cli import validation as _validation


def run(args):
    """Compatibility wrapper preserving runtime monkeypatches of executor hooks."""
    previous = _run_commands.execute_with_limits
    _run_commands.execute_with_limits = execute_with_limits
    try:
        return _run_commands.run(args)
    finally:
        _run_commands.execute_with_limits = previous


def doctor(args):
    """Compatibility wrapper preserving runtime protocol-map overrides."""
    previous_validation = _validation.PROTOCOLS
    previous_support = _core.PROTOCOLS
    _validation.PROTOCOLS = PROTOCOLS
    _core.PROTOCOLS = PROTOCOLS
    try:
        return _validation.doctor(args)
    finally:
        _validation.PROTOCOLS = previous_validation
        _core.PROTOCOLS = previous_support


def resume_command(args):
    """Compatibility wrapper preserving runtime paste-size overrides."""
    previous = _validation.DEFAULT_MAX_OUTPUT_BYTES
    _validation.DEFAULT_MAX_OUTPUT_BYTES = DEFAULT_MAX_OUTPUT_BYTES
    try:
        return _validation.resume_command(args)
    finally:
        _validation.DEFAULT_MAX_OUTPUT_BYTES = previous

def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        description="Run critic protocols without depending on Claude Code."
    )
    sub = top.add_subparsers(dest="command", required=True)

    app_parser = sub.add_parser(
        "app", help="start the complete local Argument Workbench application"
    )
    app_parser.add_argument(
        "manuscript", nargs="?", help="optional UTF-8 Markdown/TXT manuscript to import as V1"
    )
    app_parser.add_argument(
        "--project", help="existing project directory, or destination when importing a manuscript"
    )
    app_parser.add_argument("--title", help="project title for a new manuscript")
    app_parser.add_argument(
        "--data-dir", help="local project library (default: per-user application data)"
    )
    app_parser.add_argument("--host", default="127.0.0.1", help="loopback host only")
    app_parser.add_argument("--port", type=int, default=0, help="local port (default: automatic)")
    app_parser.add_argument("--no-browser", action="store_true", help="print URL without opening it")
    app_parser.set_defaults(func=app_command)

    studio_parser = sub.add_parser(
        "studio", help="start the local Document Review Studio application"
    )
    studio_parser.add_argument("--project", help="existing .document-review-studio project")
    studio_parser.add_argument("--data-dir", help="local Document Review Studio project library")
    studio_parser.add_argument("--host", default="127.0.0.1", help="loopback host only")
    studio_parser.add_argument("--port", type=int, default=0, help="port (default: automatic)")
    studio_parser.add_argument("--no-browser", action="store_true", help="print URL without opening it")
    studio_parser.set_defaults(func=studio_command)

    studio_protocols_parser = sub.add_parser(
        "studio-protocols", help="export independent AI critic protocols for a Document Review Studio project"
    )
    studio_protocols_parser.add_argument("project", help="existing .document-review-studio project")
    studio_protocols_parser.add_argument("--critic", action="append", choices=CRITIC_DIMENSIONS, help="critic to export; repeat or omit for all five")
    studio_protocols_parser.add_argument("--provider", required=True, help="provider label recorded in the request")
    studio_protocols_parser.add_argument("--model", required=True, help="model label recorded in the request")
    studio_protocols_parser.set_defaults(func=studio_protocols_command)

    studio_import_parser = sub.add_parser(
        "studio-import-ai", help="import one raw independent AI critic JSON response"
    )
    studio_import_parser.add_argument("project", help="existing .document-review-studio project")
    studio_import_parser.add_argument("critic", choices=CRITIC_DIMENSIONS)
    studio_import_parser.add_argument("file", help="UTF-8 JSON response file")
    studio_import_parser.add_argument("--provider", required=True, help="provider label bound by the exported request")
    studio_import_parser.add_argument("--model", required=True, help="model label bound by the exported request")
    studio_import_parser.add_argument("--request-id", help="specific exported AI request id")
    studio_import_parser.add_argument("--binding-mode", choices=("strict", "manual_association"), default="strict", help="strict requires response envelope; manual_association accepts ordinary JSON and records the weaker user association")
    studio_import_parser.set_defaults(func=studio_import_ai_command)

    sub.add_parser("list", help="list available protocols").set_defaults(func=list_protocols)
    sub.add_parser("tracks", help="list academic tracks and their protocols").set_defaults(
        func=list_tracks
    )
    doctor_parser = sub.add_parser(
        "doctor", help="check Python, protocol files, track mappings, and write access"
    )
    doctor_parser.add_argument(
        "--directory",
        default=".",
        help="directory to test for archive write access (default: current directory)",
    )
    doctor_parser.add_argument(
        "--repair",
        action="store_true",
        help="install missing repairable Document Review Studio Python adapters before checking",
    )
    doctor_parser.set_defaults(func=doctor)

    ir_parser = sub.add_parser(
        "ir",
        help="inspect, correct, and validate the Argument IR workflow",
    )
    ir_sub = ir_parser.add_subparsers(dest="ir_command", required=True)

    ir_init_parser = ir_sub.add_parser(
        "init",
        help="import a manuscript into a local Argument Workbench V1 project",
    )
    ir_init_parser.add_argument("manuscript", help="UTF-8 manuscript path")
    ir_init_parser.add_argument(
        "--project-dir",
        help="project directory (default: <manuscript>.argument-workbench beside source)",
    )
    ir_init_parser.add_argument("--title", help="project/document title (default: filename stem)")
    ir_init_parser.set_defaults(func=ir_init_command)

    ir_ui_parser = ir_sub.add_parser(
        "ui",
        help="open the local document-first Argument Workbench application",
    )
    ir_ui_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_ui_parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="loopback host only (default: 127.0.0.1)",
    )
    ir_ui_parser.add_argument(
        "--port", type=int, default=0, help="local port (default: choose a free port)"
    )
    ir_ui_parser.add_argument(
        "--no-browser", action="store_true", help="print the URL without opening a browser"
    )
    ir_ui_parser.set_defaults(func=ir_ui_command)

    ir_import_version_parser = ir_sub.add_parser(
        "import-version",
        help="append a new immutable manuscript DocumentVersion",
    )
    ir_import_version_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_import_version_parser.add_argument("manuscript", help="new UTF-8 manuscript path")
    ir_import_version_parser.add_argument(
        "--parent-version",
        help="parent Version ID (default: current latest; branching is not yet supported)",
    )
    ir_import_version_parser.set_defaults(func=ir_import_version_command)

    ir_diff_versions_parser = ir_sub.add_parser(
        "diff-versions",
        help="derive exact source/IR structural changes without semantic lineage",
    )
    ir_diff_versions_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_diff_versions_parser.add_argument(
        "--from-version", help="parent Version ID (default: parent of to-version)"
    )
    ir_diff_versions_parser.add_argument(
        "--to-version", help="descendant Version ID (default: latest)"
    )
    ir_diff_versions_parser.set_defaults(func=ir_diff_versions_command)

    ir_lineage_parser = ir_sub.add_parser(
        "lineage",
        help="prepare, collect, and inspect semantic Claim lineage proposals",
    )
    ir_lineage_sub = ir_lineage_parser.add_subparsers(
        dest="ir_lineage_command", required=True
    )
    ir_lineage_prepare_parser = ir_lineage_sub.add_parser(
        "prepare", help="snapshot two Reviewed IRs and prepare a model-neutral lineage prompt"
    )
    ir_lineage_prepare_parser.add_argument("project", help="Argument Workbench project directory")
    ir_lineage_prepare_parser.add_argument("--from-version")
    ir_lineage_prepare_parser.add_argument("--to-version")
    ir_lineage_prepare_parser.set_defaults(func=ir_lineage_prepare_command)

    ir_lineage_collect_parser = ir_lineage_sub.add_parser(
        "collect", help="immutably collect a model's semantic lineage proposal"
    )
    ir_lineage_collect_parser.add_argument("project", help="Argument Workbench project directory")
    ir_lineage_collect_source = ir_lineage_collect_parser.add_mutually_exclusive_group(required=True)
    ir_lineage_collect_source.add_argument("--paste", action="store_true")
    ir_lineage_collect_source.add_argument("--file")
    ir_lineage_collect_parser.add_argument("--from-version")
    ir_lineage_collect_parser.add_argument("--to-version")
    ir_lineage_collect_parser.add_argument("--analysis-id")
    ir_lineage_collect_parser.add_argument("--producer-label")
    ir_lineage_collect_parser.set_defaults(func=ir_lineage_collect_command)

    ir_lineage_show_parser = ir_lineage_sub.add_parser(
        "show", help="show the latest valid semantic lineage proposal without opening JSON"
    )
    ir_lineage_show_parser.add_argument("project", help="Argument Workbench project directory")
    ir_lineage_show_parser.add_argument("--from-version")
    ir_lineage_show_parser.add_argument("--to-version")
    ir_lineage_show_parser.add_argument("--analysis-id")
    ir_lineage_show_parser.set_defaults(func=ir_lineage_show_command)

    ir_lineage_adjudicate_parser = ir_lineage_sub.add_parser(
        "adjudicate", help="append human confirm/reject/correct decisions without editing JSON"
    )
    ir_lineage_adjudicate_parser.add_argument("project", help="Argument Workbench project directory")
    lineage_targets = ir_lineage_adjudicate_parser.add_mutually_exclusive_group(required=True)
    lineage_targets.add_argument("--proposal", action="append", help="proposal ID such as LP1; repeatable")
    lineage_targets.add_argument("--all", action="store_true", help="apply one confirm/reject decision to every proposal")
    ir_lineage_adjudicate_parser.add_argument("--expected-count", type=int, help="required safety count with --all")
    ir_lineage_adjudicate_parser.add_argument("--decision", choices=("confirm", "reject", "correct"), required=True)
    ir_lineage_adjudicate_parser.add_argument("--reason", required=True, help="human decision reason")
    ir_lineage_adjudicate_parser.add_argument("--from-version")
    ir_lineage_adjudicate_parser.add_argument("--to-version")
    ir_lineage_adjudicate_parser.add_argument("--analysis-id")
    ir_lineage_adjudicate_parser.add_argument("--from-claim", action="append")
    ir_lineage_adjudicate_parser.add_argument("--to-claim", action="append")
    ir_lineage_adjudicate_parser.add_argument("--relation", choices=LINEAGE_RELATIONS)
    ir_lineage_adjudicate_parser.add_argument("--semantic-change", action="append")
    ir_lineage_adjudicate_parser.add_argument("--lineage-reason")
    ir_lineage_adjudicate_parser.add_argument("--basis-ref", action="append")
    ir_lineage_adjudicate_parser.add_argument("--uncertainty", default="")
    ir_lineage_adjudicate_parser.set_defaults(func=ir_lineage_adjudicate_command)

    ir_lineage_history_parser = ir_lineage_sub.add_parser(
        "history", help="show model proposals and current human decisions together"
    )
    ir_lineage_history_parser.add_argument("project", help="Argument Workbench project directory")
    ir_lineage_history_parser.add_argument("--from-version")
    ir_lineage_history_parser.add_argument("--to-version")
    ir_lineage_history_parser.add_argument("--analysis-id")
    ir_lineage_history_parser.set_defaults(func=ir_lineage_history_command)

    ir_resolve_parser = ir_sub.add_parser(
        "resolve", help="retest accepted Findings against descendant Claims with the original Lens"
    )
    ir_resolve_sub = ir_resolve_parser.add_subparsers(dest="ir_resolve_command", required=True)
    ir_resolve_prepare_parser = ir_resolve_sub.add_parser("prepare", help="prepare an exact original-Lens retest")
    ir_resolve_prepare_parser.add_argument("project"); ir_resolve_prepare_parser.add_argument("finding_id")
    ir_resolve_prepare_parser.add_argument("--from-version", required=True); ir_resolve_prepare_parser.add_argument("--to-version", required=True)
    ir_resolve_prepare_parser.add_argument("--lineage-decision-id"); ir_resolve_prepare_parser.set_defaults(func=ir_resolve_prepare_command)
    ir_resolve_collect_parser = ir_resolve_sub.add_parser("collect", help="collect exact original-Lens retest results")
    ir_resolve_collect_parser.add_argument("project"); source = ir_resolve_collect_parser.add_mutually_exclusive_group(required=True); source.add_argument("--paste", action="store_true"); source.add_argument("--file")
    ir_resolve_collect_parser.add_argument("--resolution-id"); ir_resolve_collect_parser.add_argument("--producer-label"); ir_resolve_collect_parser.set_defaults(func=ir_resolve_collect_command)
    ir_resolve_decide_parser = ir_resolve_sub.add_parser("decide", help="human-confirm or correct the proposed resolution")
    ir_resolve_decide_parser.add_argument("project"); ir_resolve_decide_parser.add_argument("--resolution-id")
    ir_resolve_decide_parser.add_argument("--decision", choices=("confirm", "reject", "correct"), required=True); ir_resolve_decide_parser.add_argument("--reason", required=True)
    ir_resolve_decide_parser.add_argument("--final-status", choices=RESOLUTION_STATUSES); ir_resolve_decide_parser.set_defaults(func=ir_resolve_decide_command)
    ir_resolve_show_parser = ir_resolve_sub.add_parser("show", help="show the full Finding Resolution chain")
    ir_resolve_show_parser.add_argument("project"); ir_resolve_show_parser.add_argument("--resolution-id"); ir_resolve_show_parser.set_defaults(func=ir_resolve_show_command)

    ir_citations_parser = ir_sub.add_parser(
        "citations",
        help="verify Citation -> Evidence -> Claim provenance without declaring Claims false",
    )
    ir_citations_sub = ir_citations_parser.add_subparsers(
        dest="ir_citations_command", required=True
    )
    ir_citations_prepare = ir_citations_sub.add_parser(
        "prepare", help="prepare a substantive Citation verification prompt"
    )
    ir_citations_prepare.add_argument("project")
    ir_citations_prepare.add_argument("--version")
    ir_citations_prepare.add_argument(
        "--citation",
        action="append",
        help="repeatable Citation ID such as Z1 (default: every Citation in the version)",
    )
    ir_citations_prepare.set_defaults(func=ir_citations_prepare_command)

    ir_citations_collect = ir_citations_sub.add_parser(
        "collect", help="immutably collect a model Citation verification result"
    )
    ir_citations_collect.add_argument("project")
    citation_source = ir_citations_collect.add_mutually_exclusive_group(required=True)
    citation_source.add_argument("--paste", action="store_true")
    citation_source.add_argument("--file")
    ir_citations_collect.add_argument("--version")
    ir_citations_collect.add_argument("--audit-id")
    ir_citations_collect.add_argument("--producer-label")
    ir_citations_collect.set_defaults(func=ir_citations_collect_command)

    ir_citations_decide = ir_citations_sub.add_parser(
        "decide", help="confirm, reject, or correct one Citation proposal"
    )
    ir_citations_decide.add_argument("project")
    ir_citations_decide.add_argument("--version")
    ir_citations_decide.add_argument("--audit-id")
    ir_citations_decide.add_argument("--citation", required=True)
    ir_citations_decide.add_argument(
        "--decision", choices=("confirm", "reject", "correct"), required=True
    )
    ir_citations_decide.add_argument("--reason", required=True)
    ir_citations_decide.add_argument(
        "--bibliographic-existence", choices=CITATION_BIBLIOGRAPHIC_STATUSES
    )
    ir_citations_decide.add_argument(
        "--exact-source-located", choices=CITATION_SOURCE_LOCATION_STATUSES
    )
    ir_citations_decide.add_argument(
        "--content-support", choices=CITATION_CONTENT_SUPPORT_STATUSES
    )
    ir_citations_decide.add_argument(
        "--context-preserved", choices=CITATION_CONTEXT_STATUSES
    )
    ir_citations_decide.add_argument("--uncertainty", default="")
    ir_citations_decide.add_argument("--producer-label")
    ir_citations_decide.set_defaults(func=ir_citations_decide_command)

    ir_citations_show = ir_citations_sub.add_parser(
        "show", help="show Citation outcomes and downstream dependency flags"
    )
    ir_citations_show.add_argument("project")
    ir_citations_show.add_argument("--version")
    ir_citations_show.add_argument("--audit-id")
    ir_citations_show.set_defaults(func=ir_citations_show_command)

    ir_citations_rebuild = ir_citations_sub.add_parser(
        "rebuild", help="rebuild Citation provenance indexes and Markdown"
    )
    ir_citations_rebuild.add_argument("project")
    ir_citations_rebuild.set_defaults(func=ir_citations_rebuild_command)

    ir_gate_b_parser = ir_sub.add_parser(
        "gate-b", help="capture human Product Gate B evidence for real multi-version writing"
    )
    ir_gate_b_sub = ir_gate_b_parser.add_subparsers(dest="ir_gate_b_command", required=True)
    gate_b_init = ir_gate_b_sub.add_parser("init", help="bind 2-3 completed real multi-version projects")
    gate_b_init.add_argument("output"); gate_b_init.add_argument("projects", nargs="+"); gate_b_init.set_defaults(func=ir_gate_b_init_command)
    gate_b_assess = ir_gate_b_sub.add_parser("assess", help="append one human multi-version usability assessment")
    gate_b_assess.add_argument("gate"); gate_b_assess.add_argument("project"); gate_b_assess.add_argument("--lineage-correction-minutes", type=int, required=True)
    for field in ("lineage-reasonable", "split-merge-worked", "finding-inheritance-correct", "resolved-stopped-reappearing", "unresolved-persisted"):
        gate_b_assess.add_argument("--" + field, choices=GATE_B_JUDGMENTS, required=True)
    gate_b_assess.add_argument("--revision-rationale-clarity", choices=GATE_B_CLARITIES, required=True); gate_b_assess.add_argument("--notes", default=""); gate_b_assess.set_defaults(func=ir_gate_b_assess_command)
    gate_b_report = ir_gate_b_sub.add_parser("report", help="rebuild deterministic Gate B report")
    gate_b_report.add_argument("gate"); gate_b_report.add_argument("--show", action="store_true"); gate_b_report.set_defaults(func=ir_gate_b_report_command)
    gate_b_decide = ir_gate_b_sub.add_parser("decide", help="append a human Gate B pass/fail/defer decision")
    gate_b_decide.add_argument("gate"); gate_b_decide.add_argument("decision", choices=GATE_B_DECISIONS); gate_b_decide.add_argument("--reason", required=True); gate_b_decide.set_defaults(func=ir_gate_b_decide_command)
    gate_b_verify = ir_gate_b_sub.add_parser("verify", help="verify Gate B bindings and report bytes")
    gate_b_verify.add_argument("gate"); gate_b_verify.set_defaults(func=ir_gate_b_verify_command)

    ir_collect_parser = ir_sub.add_parser(
        "collect",
        help="immutably collect a model's Raw Argument IR response",
    )
    ir_collect_parser.add_argument("project", help="Argument Workbench project directory")
    ir_collect_source = ir_collect_parser.add_mutually_exclusive_group(required=True)
    ir_collect_source.add_argument(
        "--paste", action="store_true", help=f"paste JSON until {IR_PASTE_END_MARKER}"
    )
    ir_collect_source.add_argument("--file", help="existing Raw IR response file")
    ir_collect_parser.add_argument(
        "--producer-label",
        help="opaque model/executor label for provenance; no provider SDK is required",
    )
    ir_collect_parser.set_defaults(func=ir_collect_command)

    ir_inspect_parser = ir_sub.add_parser(
        "inspect",
        help="view and interactively correct Raw IR without editing JSON",
    )
    ir_inspect_parser.add_argument("project", help="Argument Workbench project directory")
    ir_inspect_parser.add_argument(
        "--view-only",
        action="store_true",
        help="print the current structure without starting the correction menu",
    )
    ir_inspect_parser.set_defaults(func=ir_inspect_command)

    ir_rebuild_parser = ir_sub.add_parser(
        "rebuild",
        help="deterministically rebuild Reviewed IR and argument-map.md",
    )
    ir_rebuild_parser.add_argument("project", help="Argument Workbench project directory")
    ir_rebuild_parser.set_defaults(func=ir_rebuild_command)

    ir_verify_project_parser = ir_sub.add_parser(
        "verify-project",
        help="verify every Workbench artifact, parent hash, and derived byte",
    )
    ir_verify_project_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_verify_project_parser.set_defaults(func=ir_verify_project_command)

    ir_review_parser = ir_sub.add_parser(
        "review",
        help="run claim-centered Review Lenses inside an Argument Workbench project",
    )
    ir_review_sub = ir_review_parser.add_subparsers(
        dest="ir_review_command", required=True
    )

    ir_review_prepare_parser = ir_review_sub.add_parser(
        "prepare",
        help="prepare an IR-native Rule Lens plan against Reviewed IR",
    )
    ir_review_prepare_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_review_prepare_parser.add_argument(
        "--rules",
        default=str(IR_SOCIAL_SCIENCE_RULES),
        help="check-library JSON path (default: bundled social-science rules)",
    )
    ir_review_prepare_parser.add_argument(
        "--depth",
        choices=("core", "full"),
        default="core",
        help="core checks only, or core plus extended checks (default: core)",
    )
    ir_review_prepare_parser.add_argument(
        "--scope",
        choices=("thesis-chain", "claim", "claims", "all"),
        default="thesis-chain",
        help="Claims to review (default: conclusion/intermediate support chain)",
    )
    ir_review_prepare_parser.add_argument(
        "--claim",
        action="append",
        default=[],
        help="Claim ID used by claim/claims scope, or pinned into thesis-chain",
    )
    ir_review_prepare_parser.set_defaults(func=ir_review_prepare_command)

    ir_review_collect_parser = ir_review_sub.add_parser(
        "collect",
        help="immutably collect and validate a Rule Lens model result",
    )
    ir_review_collect_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_review_collect_source = ir_review_collect_parser.add_mutually_exclusive_group(
        required=True
    )
    ir_review_collect_source.add_argument(
        "--paste",
        action="store_true",
        help=f"paste JSON until {IR_PASTE_END_MARKER}",
    )
    ir_review_collect_source.add_argument(
        "--file", help="existing argument-check-results JSON file"
    )
    ir_review_collect_parser.add_argument(
        "--review-id",
        help="Rule Review ID (default: most recently prepared review)",
    )
    ir_review_collect_parser.add_argument(
        "--producer-label",
        help="opaque model/executor label for provenance",
    )
    ir_review_collect_parser.set_defaults(func=ir_review_collect_command)

    ir_review_prepare_perspective_parser = ir_review_sub.add_parser(
        "prepare-perspective",
        help="prepare a holistic Perspective Lens review against Reviewed IR",
    )
    ir_review_prepare_perspective_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_review_prepare_perspective_parser.add_argument(
        "--lens",
        choices=("methodological-individualism", "contrastive-explanation"),
        required=True,
        help="complete methodological framework to apply",
    )
    ir_review_prepare_perspective_parser.add_argument(
        "--scope",
        choices=("thesis-chain", "claim", "claims", "all"),
        default="thesis-chain",
        help="Claims to review (default: conclusion/intermediate support chain)",
    )
    ir_review_prepare_perspective_parser.add_argument(
        "--claim",
        action="append",
        default=[],
        help="Claim ID used by claim/claims scope, or pinned into thesis-chain",
    )
    ir_review_prepare_perspective_parser.set_defaults(
        func=ir_review_prepare_perspective_command
    )

    ir_review_collect_perspective_parser = ir_review_sub.add_parser(
        "collect-perspective",
        help="immutably collect and normalize a Perspective Lens model result",
    )
    ir_review_collect_perspective_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    perspective_source = (
        ir_review_collect_perspective_parser.add_mutually_exclusive_group(
            required=True
        )
    )
    perspective_source.add_argument(
        "--paste",
        action="store_true",
        help=f"paste JSON until {IR_PASTE_END_MARKER}",
    )
    perspective_source.add_argument(
        "--file", help="existing perspective-lens-results JSON file"
    )
    ir_review_collect_perspective_parser.add_argument(
        "--review-id",
        help="Perspective Review ID (default: most recently prepared review)",
    )
    ir_review_collect_perspective_parser.add_argument(
        "--producer-label", help="opaque model/executor label for provenance"
    )
    ir_review_collect_perspective_parser.set_defaults(
        func=ir_review_collect_perspective_command
    )

    ir_review_show_parser = ir_review_sub.add_parser(
        "show",
        help="show every check outcome and open Finding for a Claim",
    )
    ir_review_show_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_review_show_parser.add_argument(
        "--review-id",
        help="Rule Review ID (default: most recent review with valid results)",
    )
    ir_review_show_parser.add_argument(
        "--claim",
        help="Claim ID such as C4 or V1:C4 (default: show all reviewed Claims)",
    )
    ir_review_show_parser.set_defaults(func=ir_review_show_command)

    ir_review_show_perspective_parser = ir_review_sub.add_parser(
        "show-perspective",
        help="show one holistic Perspective Lens outcome per selected Claim",
    )
    ir_review_show_perspective_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_review_show_perspective_parser.add_argument(
        "--review-id",
        help="Perspective Review ID (default: most recent valid result)",
    )
    ir_review_show_perspective_parser.add_argument(
        "--claim", help="Claim ID such as C4 or V1:C4"
    )
    ir_review_show_perspective_parser.set_defaults(
        func=ir_review_show_perspective_command
    )

    ir_review_show_claim_lenses_parser = ir_review_sub.add_parser(
        "show-claim-lenses",
        help="show current Rule and Perspective Lens outcomes without synthesis",
    )
    ir_review_show_claim_lenses_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_review_show_claim_lenses_parser.add_argument(
        "--claim", required=True, help="Claim ID such as C4 or V1:C4"
    )
    ir_review_show_claim_lenses_parser.set_defaults(
        func=ir_review_show_claim_lenses_command
    )

    ir_review_triage_parser = ir_review_sub.add_parser(
        "triage",
        help="acknowledge or reject non-substantive model execution statuses",
    )
    ir_review_triage_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_review_triage_parser.add_argument(
        "--review-id",
        help="Rule Review ID (default: most recent review with valid results)",
    )
    ir_review_triage_parser.add_argument(
        "--task", help="non-evaluated task ID such as T4"
    )
    ir_review_triage_parser.add_argument(
        "--decision", choices=("acknowledge", "reject")
    )
    ir_review_triage_parser.add_argument(
        "--action",
        choices=(
            "correct_ir",
            "add_context",
            "add_evidence",
            "acknowledge_not_applicable",
            "rerun_review",
            "other",
        ),
    )
    ir_review_triage_parser.add_argument(
        "--note", help="required human explanation for a triage decision"
    )
    ir_review_triage_parser.add_argument(
        "--producer-label", help="human evaluator label for provenance"
    )
    ir_review_triage_parser.set_defaults(func=ir_review_triage_command)

    ir_adjudicate_parser = ir_sub.add_parser(
        "adjudicate",
        help="accept, reject, or defer Claim-level Workbench Findings",
    )
    ir_adjudicate_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_adjudicate_parser.add_argument(
        "--review-id",
        help="limit decisions to one current Rule or Perspective Review",
    )
    ir_adjudicate_parser.add_argument(
        "--review-all",
        action="store_true",
        help="include Findings that already have a human decision",
    )
    ir_adjudicate_parser.add_argument(
        "--view-only",
        action="store_true",
        help="show current human decisions without starting the prompt",
    )
    ir_adjudicate_parser.add_argument(
        "--summary-only",
        action="store_true",
        help="show grouped open counts without listing or deciding Findings",
    )
    ir_adjudicate_parser.add_argument(
        "--group-by-claim",
        action="store_true",
        help="show open Findings as Claim-level confirmation bundles without writing",
    )
    ir_adjudicate_parser.add_argument(
        "--verdict",
        choices=("fail", "uncertain"),
        help="show or adjudicate only model FAIL or UNCERTAIN Findings",
    )
    ir_adjudicate_parser.add_argument(
        "--claim",
        help="show or adjudicate one target Claim such as C4 or V1:C4",
    )
    ir_adjudicate_parser.add_argument(
        "--check",
        dest="check_id",
        help="show or adjudicate one exact Rule Lens check ID",
    )
    ir_adjudicate_parser.add_argument(
        "--batch-decision",
        choices=("accept", "reject", "defer"),
        help="apply one explicit human choice to the exact open Findings of --claim",
    )
    ir_adjudicate_parser.add_argument(
        "--reason",
        help="required human reason for a batch decision",
    )
    ir_adjudicate_parser.add_argument(
        "--confirm-count",
        type=int,
        help="required optimistic-lock count from the inspected Claim bundle",
    )
    ir_adjudicate_parser.add_argument(
        "--action",
        action="append",
        help="repeatable ACTION_TYPE:TEXT; required for accept and forbidden otherwise",
    )
    ir_adjudicate_parser.add_argument(
        "--producer-label",
        help="human evaluator label recorded in batch-decision provenance",
    )
    ir_adjudicate_parser.set_defaults(func=ir_adjudicate_command)

    ir_revision_plan_parser = ir_sub.add_parser(
        "revision-plan",
        help="deterministically rebuild the Workbench revision plan",
    )
    ir_revision_plan_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_revision_plan_parser.add_argument(
        "--show",
        action="store_true",
        help="print revision-plan.md after rebuilding it",
    )
    ir_revision_plan_parser.set_defaults(func=ir_revision_plan_command)

    ir_gate_a_parser = ir_sub.add_parser(
        "gate-a",
        help="evaluate Phase 1-3 on a private corpus of 3-5 real manuscripts",
    )
    ir_gate_a_sub = ir_gate_a_parser.add_subparsers(
        dest="ir_gate_a_command", required=True
    )
    ir_gate_a_readiness_parser = ir_gate_a_sub.add_parser(
        "readiness",
        help="show read-only progress and next commands for 3-5 Workbench projects",
    )
    ir_gate_a_readiness_parser.add_argument(
        "projects", nargs="+", help="3-5 real-manuscript Workbench projects"
    )
    ir_gate_a_readiness_parser.set_defaults(func=ir_gate_a_readiness_command)

    ir_gate_a_prepare_baseline_parser = ir_gate_a_sub.add_parser(
        "prepare-baseline",
        help="create a deterministic full-manuscript direct-review prompt",
    )
    ir_gate_a_prepare_baseline_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_gate_a_prepare_baseline_parser.add_argument(
        "output", help="new UTF-8 prompt path outside the Workbench"
    )
    ir_gate_a_prepare_baseline_parser.set_defaults(
        func=ir_gate_a_prepare_baseline_command
    )

    ir_gate_a_session_parser = ir_gate_a_sub.add_parser(
        "session", help="record actual human Gate A work time"
    )
    ir_gate_a_session_sub = ir_gate_a_session_parser.add_subparsers(
        dest="ir_gate_a_session_command", required=True
    )
    ir_gate_a_session_start_parser = ir_gate_a_session_sub.add_parser(
        "start", help="append a system-timed session start artifact"
    )
    ir_gate_a_session_start_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_gate_a_session_start_parser.add_argument(
        "--activity", choices=GATE_A_WORK_ACTIVITIES, required=True
    )
    ir_gate_a_session_start_parser.add_argument("--note", default="")
    ir_gate_a_session_start_parser.add_argument(
        "--producer-label", default="local-user"
    )
    ir_gate_a_session_start_parser.set_defaults(
        func=ir_gate_a_session_start_command
    )
    ir_gate_a_session_finish_parser = ir_gate_a_session_sub.add_parser(
        "finish", help="append completion and deterministic elapsed time"
    )
    ir_gate_a_session_finish_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_gate_a_session_finish_parser.add_argument(
        "session", help="session ID such as GS1"
    )
    ir_gate_a_session_finish_parser.add_argument(
        "--producer-label", default="local-user"
    )
    ir_gate_a_session_finish_parser.set_defaults(
        func=ir_gate_a_session_finish_command
    )
    ir_gate_a_session_abandon_parser = ir_gate_a_session_sub.add_parser(
        "abandon", help="close an interrupted interval without counting it as work"
    )
    ir_gate_a_session_abandon_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_gate_a_session_abandon_parser.add_argument(
        "session", help="session ID such as GS1"
    )
    ir_gate_a_session_abandon_parser.add_argument(
        "--reason", required=True, help="human-confirmed reason for abandoning the interval"
    )
    ir_gate_a_session_abandon_parser.add_argument(
        "--producer-label", default="local-user"
    )
    ir_gate_a_session_abandon_parser.set_defaults(
        func=ir_gate_a_session_abandon_command
    )
    ir_gate_a_session_list_parser = ir_gate_a_session_sub.add_parser(
        "list", help="show completed and open human work sessions"
    )
    ir_gate_a_session_list_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_gate_a_session_list_parser.set_defaults(
        func=ir_gate_a_session_list_command
    )

    ir_gate_a_baseline_parser = ir_gate_a_sub.add_parser(
        "baseline",
        help="immutably collect a direct full-text chat comparison",
    )
    ir_gate_a_baseline_parser.add_argument(
        "project", help="Argument Workbench project directory"
    )
    ir_gate_a_baseline_parser.add_argument(
        "--prompt-file", required=True, help="exact UTF-8 direct-chat prompt"
    )
    ir_gate_a_baseline_parser.add_argument(
        "--response-file", required=True, help="exact UTF-8 model response"
    )
    ir_gate_a_baseline_parser.add_argument(
        "--model-label", required=True, help="human-supplied model/version label"
    )
    ir_gate_a_baseline_parser.add_argument(
        "--model-provider", required=True, help="human-supplied provider name"
    )
    ir_gate_a_baseline_parser.add_argument(
        "--model-id", required=True, help="provider model identifier"
    )
    ir_gate_a_baseline_parser.add_argument(
        "--interaction-mode",
        choices=BASELINE_INTERACTION_MODES,
        required=True,
        help="whether the comparison ran in a fresh conversation",
    )
    ir_gate_a_baseline_parser.add_argument(
        "--prior-context",
        choices=BASELINE_PRIOR_CONTEXTS,
        required=True,
        help="context present before the comparison prompt",
    )
    ir_gate_a_baseline_parser.add_argument(
        "--manuscript-delivery",
        choices=BASELINE_MANUSCRIPT_DELIVERY,
        required=True,
        help="how the complete manuscript was supplied to the model",
    )
    ir_gate_a_baseline_parser.add_argument(
        "--full-manuscript-confirmed",
        action="store_true",
        help="human confirmation that the model received the complete manuscript",
    )
    ir_gate_a_baseline_parser.add_argument(
        "--started-at", required=True, help="timezone-aware ISO start time"
    )
    ir_gate_a_baseline_parser.add_argument(
        "--completed-at", required=True, help="timezone-aware ISO completion time"
    )
    ir_gate_a_baseline_parser.add_argument(
        "--producer-label",
        default="direct-chat-model",
        help="provenance label for the response producer",
    )
    ir_gate_a_baseline_parser.set_defaults(func=ir_gate_a_baseline_command)

    ir_gate_a_init_parser = ir_gate_a_sub.add_parser(
        "init", help="capture exact hashes for 3-5 completed Workbench projects"
    )
    ir_gate_a_init_parser.add_argument("output", help="new local Gate A evidence directory")
    ir_gate_a_init_parser.add_argument(
        "projects", nargs="+", help="3-5 completed real-manuscript Workbench projects"
    )
    ir_gate_a_init_parser.set_defaults(func=ir_gate_a_init_command)

    ir_gate_a_assess_parser = ir_gate_a_sub.add_parser(
        "assess", help="append one human usability/IR-quality assessment"
    )
    ir_gate_a_assess_parser.add_argument("gate", help="Gate A evidence directory")
    ir_gate_a_assess_parser.add_argument("project", help="corpus alias such as P1")
    ir_gate_a_assess_parser.add_argument(
        "--comparison", choices=GATE_A_COMPARISONS, required=True,
        help="clarity/control compared with direct full-text chat review",
    )
    ir_gate_a_assess_parser.add_argument(
        "--burden", choices=GATE_A_BURDENS, required=True,
        help="human IR correction burden",
    )
    for metric in GATE_A_METRIC_KEYS:
        ir_gate_a_assess_parser.add_argument(
            "--" + metric.replace("_", "-"), type=int, required=True
        )
    ir_gate_a_assess_parser.add_argument(
        "--correction-minutes",
        type=int,
        help=(
            "legacy v1-v4 corpus only; v5 derives exact inspection time from "
            "bound work-session artifacts"
        ),
    )
    ir_gate_a_assess_parser.add_argument(
        "--anchor",
        action="append",
        required=True,
        help="known important Claim, extraction trap, Finding, or framework reversal; repeatable",
    )
    ir_gate_a_assess_parser.add_argument(
        "--actual-revision-notes",
        default="",
        help="what the author actually revised, if observed during Gate A",
    )
    ir_gate_a_assess_parser.add_argument("--notes", default="", help="free-form evaluator notes")
    ir_gate_a_assess_parser.set_defaults(func=ir_gate_a_assess_command)

    ir_gate_a_report_parser = ir_gate_a_sub.add_parser(
        "report", help="rebuild the deterministic Gate A evidence report"
    )
    ir_gate_a_report_parser.add_argument("gate", help="Gate A evidence directory")
    ir_gate_a_report_parser.add_argument("--show", action="store_true", help="print the report")
    ir_gate_a_report_parser.set_defaults(func=ir_gate_a_report_command)

    ir_gate_a_decide_parser = ir_gate_a_sub.add_parser(
        "decide", help="append a human pass/fail/defer gate decision"
    )
    ir_gate_a_decide_parser.add_argument("gate", help="Gate A evidence directory")
    ir_gate_a_decide_parser.add_argument("decision", choices=GATE_A_DECISIONS)
    ir_gate_a_decide_parser.add_argument("--reason", required=True, help="human decision rationale")
    ir_gate_a_decide_parser.set_defaults(func=ir_gate_a_decide_command)

    ir_gate_a_verify_parser = ir_gate_a_sub.add_parser(
        "verify", help="verify corpus bindings, assessments, decisions, and report bytes"
    )
    ir_gate_a_verify_parser.add_argument("gate", help="Gate A evidence directory")
    ir_gate_a_verify_parser.set_defaults(func=ir_gate_a_verify_command)

    ir_prepare_parser = ir_sub.add_parser(
        "prepare",
        help="create a source-bound prompt for extracting Argument IR",
    )
    ir_prepare_parser.add_argument("manuscript", help="UTF-8 manuscript path")
    ir_prepare_parser.add_argument(
        "--output", help="output prompt path (default: beside manuscript)"
    )
    ir_prepare_parser.set_defaults(func=ir_prepare_command)

    ir_validate_parser = ir_sub.add_parser(
        "validate",
        help="validate an Argument IR against the exact manuscript bytes",
    )
    ir_validate_parser.add_argument("manuscript", help="UTF-8 manuscript path")
    ir_validate_parser.add_argument("argument_ir", help="Argument IR JSON path")
    ir_validate_parser.set_defaults(func=ir_validate_command)

    ir_plan_parser = ir_sub.add_parser(
        "plan",
        help="select method-conditional checks and create an execution prompt",
    )
    ir_plan_parser.add_argument("manuscript", help="UTF-8 manuscript path")
    ir_plan_parser.add_argument("argument_ir", help="validated Argument IR JSON path")
    ir_plan_parser.add_argument(
        "--rules",
        default=str(IR_SOCIAL_SCIENCE_RULES),
        help="check-library JSON path (default: bundled social-science rules)",
    )
    ir_plan_parser.add_argument(
        "--depth",
        choices=("core", "full"),
        default="core",
        help="core checks only, or core plus extended checks (default: core)",
    )
    ir_plan_parser.add_argument(
        "--scope",
        choices=("thesis-chain", "claim", "claims", "all"),
        default="thesis-chain",
        help="Claims to review (default: conclusion/intermediate support chain)",
    )
    ir_plan_parser.add_argument(
        "--claim",
        action="append",
        default=[],
        help="Claim ID used by claim/claims scope, or pinned into thesis-chain",
    )
    ir_plan_parser.add_argument(
        "--output", help="check-plan JSON path (default: beside Argument IR)"
    )
    ir_plan_parser.add_argument(
        "--prompt-output",
        help="execution prompt path (default: beside Argument IR)",
    )
    ir_plan_parser.set_defaults(func=ir_plan_command)

    ir_results_parser = ir_sub.add_parser(
        "validate-results",
        help="validate model results against the exact check-plan bytes",
    )
    ir_results_parser.add_argument("check_plan", help="check-plan JSON path")
    ir_results_parser.add_argument("results", help="check-results JSON path")
    ir_results_parser.add_argument(
        "--rules",
        default=str(IR_SOCIAL_SCIENCE_RULES),
        help="check-library JSON path used to reproduce the plan (default: bundled rules)",
    )
    ir_results_parser.set_defaults(func=ir_validate_results_command)

    ir_findings_parser = ir_sub.add_parser(
        "findings",
        help="derive deterministic fail/uncertain findings from validated results",
    )
    ir_findings_parser.add_argument("check_plan", help="check-plan JSON path")
    ir_findings_parser.add_argument("results", help="validated check-results JSON path")
    ir_findings_parser.add_argument(
        "--rules",
        default=str(IR_SOCIAL_SCIENCE_RULES),
        help="check-library JSON path used to reproduce the plan (default: bundled rules)",
    )
    ir_findings_parser.add_argument(
        "--output", help="findings JSON path (default: beside results)"
    )
    ir_findings_parser.set_defaults(func=ir_findings_command)

    quickstart_parser = sub.add_parser(
        "quickstart", help="中文交互引导：选择文章和学术线并生成 prompt"
    )
    quickstart_parser.add_argument(
        "manuscript", nargs="?", help="可选的 UTF-8 文章路径"
    )
    quickstart_parser.add_argument(
        "--track", choices=ACADEMIC_TRACKS, help="可选；跳过交互式学术线选择"
    )
    quickstart_parser.add_argument(
        "--runs-dir",
        default=".critic-runs",
        help="归档目录（默认：.critic-runs）",
    )
    quickstart_parser.set_defaults(func=quickstart)

    prepare_parser = sub.add_parser(
        "prepare", help="archive a self-contained prompt for manual use"
    )
    _add_run_inputs(prepare_parser)
    prepare_parser.set_defaults(func=prepare)

    prepare_track_parser = sub.add_parser(
        "prepare-track", help="prepare the primary protocol for an academic track"
    )
    _add_track_inputs(prepare_track_parser)
    prepare_track_parser.set_defaults(
        allow_test_artifact=False,
        func=prepare_track,
    )

    run_parser = sub.add_parser(
        "run", help="run one protocol through an external stdin/stdout command"
    )
    _add_run_inputs(run_parser)
    _add_execution_limits(run_parser)
    run_parser.set_defaults(func=run)

    run_track_parser = sub.add_parser(
        "run-track", help="run the primary protocol for an academic track"
    )
    _add_track_inputs(run_track_parser)
    _add_execution_limits(run_track_parser)
    run_track_parser.set_defaults(
        allow_test_artifact=False,
        func=run_track,
    )

    campaign_parser = sub.add_parser(
        "campaign",
        help="run a serial, isolated multi-protocol calibration campaign",
    )
    campaign_parser.add_argument("manuscript", help="UTF-8 manuscript path")
    campaign_parser.add_argument(
        "--protocol",
        action="append",
        choices=PROTOCOLS,
        help="protocol to include; repeat this option (default: individualist and contrastivist)",
    )
    campaign_parser.add_argument(
        "--track",
        action="append",
        choices=ACADEMIC_TRACKS,
        help="academic track to include; repeat this option; cannot combine with --protocol",
    )
    campaign_parser.add_argument(
        "--repeat",
        type=positive_integer,
        default=2,
        help="serial repetitions per protocol (default: 2)",
    )
    campaign_parser.add_argument(
        "--order-seed",
        help="reproduce the counterbalanced execution order (default: random seed)",
    )
    campaign_parser.add_argument(
        "--campaigns-dir",
        default=".critic-campaigns",
        help="campaign archive directory (default: .critic-campaigns)",
    )
    campaign_parser.add_argument(
        "--allow-test-artifact",
        action="store_true",
        help="allow critic-generic for second-stage calibration",
    )
    campaign_parser.add_argument(
        "--timeout", type=positive_seconds, default=900.0
    )
    campaign_parser.add_argument(
        "--max-output-bytes",
        type=positive_integer,
        default=DEFAULT_MAX_OUTPUT_BYTES,
    )
    campaign_parser.add_argument(
        "--executor-label",
        help="public reproducibility label for the model/configuration (never put secrets here)",
    )
    campaign_parser.set_defaults(func=campaign)

    validate_parser = sub.add_parser(
        "validate", help="validate the deterministic report structure"
    )
    validate_parser.add_argument("protocol", choices=PROTOCOLS)
    validate_parser.add_argument("report", help="UTF-8 report path")
    validate_parser.set_defaults(func=validate_command)

    verify_parser = sub.add_parser(
        "verify-run", help="verify an archived run against its manifest"
    )
    verify_parser.add_argument("run_dir", help="archived run directory")
    verify_parser.add_argument(
        "--source",
        help="optional original source file to recheck against source_sha256",
    )
    verify_parser.set_defaults(func=verify_run_command)

    status_parser = sub.add_parser(
        "status", help="中文显示历史运行进度、归档健康状态和下一步命令"
    )
    status_parser.add_argument(
        "run_dir", nargs="?", help="可选；只查看某一个运行目录"
    )
    status_parser.add_argument(
        "--runs-dir",
        default=".critic-runs",
        help="未指定 run_dir 时扫描的归档目录（默认：.critic-runs）",
    )
    status_parser.set_defaults(func=status_command)

    resume_parser = sub.add_parser(
        "resume", help="中文一键继续最新待办：回收报告、裁决或生成修改计划"
    )
    resume_parser.add_argument(
        "run_dir", nargs="?", help="可选；指定要继续的运行目录"
    )
    resume_parser.add_argument(
        "--runs-dir",
        default=".critic-runs",
        help="未指定 run_dir 时扫描的归档目录（默认：.critic-runs）",
    )
    resume_input = resume_parser.add_mutually_exclusive_group()
    resume_input.add_argument(
        "--report",
        help="prepared 运行的 AI 报告路径；省略时使用中文交互询问",
    )
    resume_input.add_argument(
        "--paste",
        action="store_true",
        help=f"直接粘贴 AI 回答，并以单独一行 {PASTE_END_MARKER} 结束",
    )
    resume_parser.set_defaults(func=resume_command)

    verify_campaign_parser = sub.add_parser(
        "verify-campaign", help="verify a campaign and every archived run"
    )
    verify_campaign_parser.add_argument("campaign_dir", help="campaign directory")
    verify_campaign_parser.add_argument(
        "--source", help="optional original source file to recheck"
    )
    verify_campaign_parser.set_defaults(func=verify_campaign_command)

    import_report_parser = sub.add_parser(
        "import-report",
        help="validate and bind a manual AI report to a prepared run",
    )
    import_report_parser.add_argument("run_dir", help="prepared run directory")
    import_report_parser.add_argument("report", help="UTF-8 report returned by the AI")
    import_report_parser.add_argument(
        "--adjudication-output",
        help="adjudication JSON path (default: inside the run directory)",
    )
    import_report_parser.set_defaults(func=import_report_command)

    adjudicate_parser = sub.add_parser(
        "adjudicate",
        help="中文交互裁决 AI 发现并自动生成修改计划",
    )
    adjudicate_parser.add_argument("run_dir", help="collected run directory")
    adjudicate_parser.add_argument(
        "--review-all",
        action="store_true",
        help="重新查看全部裁决；首次修改前自动留存当前 revision-plan.md",
    )
    adjudicate_parser.set_defaults(func=adjudicate_command)

    revision_plan_parser = sub.add_parser(
        "revision-plan",
        help="turn completed human adjudication into an actionable Markdown plan",
    )
    revision_plan_parser.add_argument("run_dir", help="collected run directory")
    revision_plan_parser.add_argument(
        "--adjudication",
        help="completed adjudication JSON path (default: inside the run directory)",
    )
    revision_plan_parser.add_argument(
        "--output",
        help="revision plan Markdown path (default: inside the run directory)",
    )
    revision_plan_parser.set_defaults(func=revision_plan_command)

    init_scorecard_parser = sub.add_parser(
        "init-scorecard", help="create a blank reproducible W/B scorecard"
    )
    init_scorecard_parser.add_argument("output", help="output JSON path")
    init_scorecard_parser.set_defaults(func=init_scorecard_command)

    score_parser = sub.add_parser(
        "score", help="calculate divergence intervals and the W/B verdict"
    )
    score_parser.add_argument("scorecard", help="completed scorecard JSON path")
    score_parser.add_argument(
        "--format", choices=("json", "markdown"), default="json"
    )
    score_parser.add_argument("--output", help="optional result file path")
    score_parser.set_defaults(func=score_command)

    blind_scorecard_parser = sub.add_parser(
        "blind-scorecard",
        help="create an identity-free pairing artifact and a separate private key",
    )
    blind_scorecard_parser.add_argument("scorecard", help="traceable scorecard path")
    blind_scorecard_parser.add_argument(
        "--output", help="reviewer JSON path (default: beside scorecard)"
    )
    blind_scorecard_parser.add_argument(
        "--key-output", help="private key JSON path (default: beside scorecard)"
    )
    blind_scorecard_parser.add_argument(
        "--seed", help="optional reproducible blind alias seed"
    )
    blind_scorecard_parser.set_defaults(func=blind_scorecard_command)

    apply_blind_parser = sub.add_parser(
        "apply-blind-scorecard",
        help="verify and merge blinded human pairings into a scorecard",
    )
    apply_blind_parser.add_argument("scorecard", help="original scorecard path")
    apply_blind_parser.add_argument(
        "blind", nargs="?", help="completed blind JSON path (default: beside scorecard)"
    )
    apply_blind_parser.add_argument(
        "--key", help="private key JSON path (default: beside scorecard)"
    )
    apply_blind_parser.add_argument(
        "--output", help="merged JSON path (default: beside scorecard)"
    )
    apply_blind_parser.set_defaults(func=apply_blind_scorecard_command)

    return top


def main(argv: list[str] | None = None) -> int:
    stdin_reconfigure = getattr(sys.stdin, "reconfigure", None)
    if stdin_reconfigure is not None:
        stdin_reconfigure(encoding="utf-8", errors="strict")
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    executor: list[str] = []
    if raw_argv and raw_argv[0] in {"run", "run-track", "campaign"} and "--" in raw_argv:
        separator = raw_argv.index("--")
        executor = raw_argv[separator + 1 :]
        raw_argv = raw_argv[:separator]

    args = parser().parse_args(raw_argv)
    if args.command in {"run", "run-track", "campaign"}:
        args.executor = executor
    try:
        gate_mutations = {
            ir_gate_a_assess_command,
            ir_gate_a_report_command,
            ir_gate_a_decide_command,
            ir_gate_b_assess_command,
            ir_gate_b_report_command,
            ir_gate_b_decide_command,
        }
        project_mutations = {
            ir_import_version_command,
            ir_diff_versions_command,
            ir_lineage_prepare_command,
            ir_lineage_collect_command,
            ir_lineage_adjudicate_command,
            ir_resolve_prepare_command,
            ir_resolve_collect_command,
            ir_resolve_decide_command,
            ir_citations_prepare_command,
            ir_citations_collect_command,
            ir_citations_decide_command,
            ir_citations_rebuild_command,
            ir_collect_command,
            ir_inspect_command,
            ir_rebuild_command,
            ir_review_prepare_command,
            ir_review_collect_command,
            ir_review_prepare_perspective_command,
            ir_review_collect_perspective_command,
            ir_review_triage_command,
            ir_adjudicate_command,
            ir_revision_plan_command,
            ir_gate_a_baseline_command,
            ir_gate_a_prepare_baseline_command,
            ir_gate_a_session_start_command,
            ir_gate_a_session_finish_command,
            ir_gate_a_session_abandon_command,
        }
        lock_root = None
        if args.func in gate_mutations:
            lock_root = args.gate
        elif args.func in project_mutations:
            lock_root = args.project
        if lock_root is None:
            return args.func(args)
        with project_mutation_lock(lock_root):
            return args.func(args)
    except (OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
