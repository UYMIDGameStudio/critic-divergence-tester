"""Holistic Perspective Lens workflows for Argument Workbench.

Perspective Lenses preserve complete methodological commitments. They do not
expand into check-by-Claim task matrices, vote with other lenses, or synthesize
their disagreements. Model judgments normalize into the existing Finding
envelope only after exact-byte collection and validation.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from argument_contracts import (
    FINDING_VERDICTS,
    PERSPECTIVE_LENSES,
    sha256_bytes,
    validate_artifact,
    validate_contract_bundle,
    validate_perspective_lens_results,
)
from argument_ir import (
    REVIEW_SCOPES,
    ArgumentIRError,
    select_review_claim_ids,
    validate_argument_ir,
)
from argument_workbench import (
    WorkspacePaths,
    WorkbenchError,
    _atomic_write,
    _parent,
    _provenance,
    _read_json,
    _write_new,
    correction_entries,
    json_bytes,
    list_attempts,
    parse_json_strict,
    utc_now,
    verify_workspace,
    workspace_paths,
)


PERSPECTIVE_REVIEW_ID_PATTERN = re.compile(r"PV([1-9][0-9]*)\Z")
ATTEMPT_ID_PATTERN = re.compile(r"attempt-([0-9]{4})\Z")
FINDING_FILE_PATTERN = re.compile(r"F([0-9]{4})\.json\Z")
COLLECTION_METHODS = {"file", "terminal-paste"}
PROTOCOLS = {
    "methodological-individualism": (
        "critic-individualist",
        "critic-individualist.md",
    ),
    "contrastive-explanation": (
        "critic-contrastivist",
        "critic-contrastivist.md",
    ),
}


@dataclass(frozen=True)
class PerspectiveReviewPaths:
    workspace: WorkspacePaths
    review_id: str

    @property
    def reviews_dir(self) -> Path:
        return self.workspace.version_dir / "perspective-reviews"

    @property
    def root(self) -> Path:
        return self.reviews_dir / self.review_id

    @property
    def record(self) -> Path:
        return self.root / "review-run.json"

    @property
    def protocol_record(self) -> Path:
        return self.root / "perspective-lens-protocol.json"

    @property
    def protocol(self) -> Path:
        return self.root / "perspective-lens.md"

    @property
    def reviewed_record(self) -> Path:
        return self.root / "reviewed-ir-record.json"

    @property
    def target_ir(self) -> Path:
        return self.root / "target-argument-ir.json"

    @property
    def plan(self) -> Path:
        return self.root / "perspective-review-plan.json"

    @property
    def prompt(self) -> Path:
        return self.root / "review-prompt.md"

    @property
    def results_dir(self) -> Path:
        return self.root / "results"

    @property
    def derived_dir(self) -> Path:
        return self.root / "derived"

    def attempt_dir(self, attempt_id: str) -> Path:
        return self.results_dir / attempt_id

    def derived_attempt_dir(self, attempt_id: str) -> Path:
        return self.derived_dir / attempt_id


def _paths(project_dir: Path | str, review_id: str) -> PerspectiveReviewPaths:
    normalized = review_id.upper()
    if PERSPECTIVE_REVIEW_ID_PATTERN.fullmatch(normalized) is None:
        raise WorkbenchError("Perspective Review ID must be PV1..PVn")
    return PerspectiveReviewPaths(workspace_paths(project_dir), normalized)


def list_perspective_reviews(
    project_dir: Path | str,
) -> list[PerspectiveReviewPaths]:
    workspace = workspace_paths(project_dir)
    reviews_dir = workspace.version_dir / "perspective-reviews"
    if not reviews_dir.exists():
        return []
    if reviews_dir.is_symlink() or not reviews_dir.is_dir():
        raise WorkbenchError(
            "perspective-reviews must be a regular non-symlink directory"
        )
    reviews: list[PerspectiveReviewPaths] = []
    for path in reviews_dir.iterdir():
        if path.is_symlink():
            raise WorkbenchError(
                f"Perspective Review directory must not be a symlink: {path}"
            )
        if path.is_dir() and PERSPECTIVE_REVIEW_ID_PATTERN.fullmatch(path.name):
            reviews.append(PerspectiveReviewPaths(workspace, path.name))
    return sorted(
        reviews,
        key=lambda item: int(
            PERSPECTIVE_REVIEW_ID_PATTERN.fullmatch(item.review_id).group(1)
        ),
    )


def selected_perspective_review(
    project_dir: Path | str, review_id: str | None = None
) -> PerspectiveReviewPaths:
    if review_id is not None:
        paths = _paths(project_dir, review_id)
        if not paths.root.is_dir() or paths.root.is_symlink():
            raise WorkbenchError(f"unknown Perspective Review: {paths.review_id}")
        return paths
    reviews = list_perspective_reviews(project_dir)
    if not reviews:
        raise WorkbenchError(
            "project has no Perspective Review; run `ir review prepare-perspective` first"
        )
    return reviews[-1]


def _next_review_id(project_dir: Path | str) -> str:
    numbers = [
        int(PERSPECTIVE_REVIEW_ID_PATTERN.fullmatch(item.review_id).group(1))
        for item in list_perspective_reviews(project_dir)
    ]
    return f"PV{max(numbers or [0]) + 1}"


def _next_attempt_id(paths: PerspectiveReviewPaths) -> str:
    numbers: list[int] = []
    if paths.results_dir.exists():
        if paths.results_dir.is_symlink() or not paths.results_dir.is_dir():
            raise WorkbenchError("Perspective Review results path must be a directory")
        for path in paths.results_dir.iterdir():
            match = ATTEMPT_ID_PATTERN.fullmatch(path.name)
            if path.is_dir() and not path.is_symlink() and match is not None:
                numbers.append(int(match.group(1)))
    return f"attempt-{max(numbers or [0]) + 1:04d}"


def _protocol_source(lens_id: str) -> tuple[str, Path, bytes]:
    if lens_id not in PERSPECTIVE_LENSES:
        raise WorkbenchError(
            "Perspective Lens must be methodological-individualism or "
            "contrastive-explanation"
        )
    legacy, filename = PROTOCOLS[lens_id]
    path = Path(__file__).resolve().parent / filename
    if path.is_symlink() or not path.is_file():
        raise WorkbenchError(f"Perspective Lens protocol is missing or unsafe: {path}")
    return legacy, path, path.read_bytes()


def render_perspective_prompt(
    plan: dict[str, Any],
    target_ir: dict[str, Any],
    protocol_text: str,
    *,
    plan_sha256: str,
) -> str:
    selected = list(plan["review_scope"]["selected_claim_ids"])
    lens_id = str(plan["lens"]["id"])
    schema_example = {
        "schema_version": 1,
        "artifact": "perspective-lens-results",
        "source": {
            "plan_sha256": plan_sha256,
            "target_ir_sha256": next(
                parent["sha256"]
                for parent in plan["parents"]
                if parent["role"] == "target-ir"
            ),
            "protocol_sha256": plan["lens"]["protocol_sha256"],
        },
        "status": "complete",
        "unverified": [],
        "results": [
            {
                "result_id": "P1",
                "target_claim": selected[0],
                "verdict": "pass | fail | uncertain",
                "reason": "concise claim-level judgment",
                "basis_refs": [selected[0]],
                "framework_analysis": "holistic application of the complete framework",
                "consequence": "required for fail/uncertain; empty for pass",
            }
        ],
    }
    return (
        "# Argument Workbench Perspective Lens execution\n\n"
        f"Lens: `{lens_id}`\n\n"
        "Apply the complete framework below as a coherent methodological position. "
        "Do not turn it into a checklist, score, vote, compromise, or generic manuscript review. "
        "Judge each selected Claim once from this lens. Preserve disagreements with other "
        "frameworks instead of anticipating a synthesis.\n\n"
        "Return only one UTF-8 JSON object matching the schema example. `basis_refs` may "
        "name any existing Claim, Evidence, Assumption, or Citation, but must include the "
        "target Claim. For `status=complete`, cover selected Claims exactly once and in order. "
        "Use `partial` with concrete `unverified` items only when some selected Claims could "
        "not be judged; use `blocked` with no results only when none can be judged.\n\n"
        f"Plan SHA-256: `{plan_sha256}`\n\n"
        "Selected Claims: " + ", ".join(selected) + "\n\n"
        "## Complete Perspective Lens protocol\n\n"
        + protocol_text.rstrip()
        + "\n\n## Required JSON shape\n\n```json\n"
        + json.dumps(schema_example, ensure_ascii=False, indent=2)
        + "\n```\n\n## Reviewed Argument IR snapshot\n\n```json\n"
        + json.dumps(target_ir, ensure_ascii=False, indent=2)
        + "\n```\n"
    )


def prepare_perspective_review(
    project_dir: Path | str,
    *,
    lens_id: str,
    review_scope: str = "thesis-chain",
    claim_ids: list[str] | None = None,
) -> tuple[PerspectiveReviewPaths, bool]:
    if review_scope not in REVIEW_SCOPES:
        raise WorkbenchError(f"review scope must be one of {REVIEW_SCOPES}")
    workspace = workspace_paths(project_dir)
    workspace_errors = verify_workspace(workspace)
    if workspace_errors:
        raise WorkbenchError(
            "Argument Workbench project is invalid: " + "; ".join(workspace_errors)
        )
    if not workspace.reviewed_payload.is_file() or workspace.reviewed_payload.is_symlink():
        raise WorkbenchError("Reviewed IR must exist before preparing a review")
    target_ir, target_ir_bytes = _read_json(workspace.reviewed_payload)
    reviewed_record, reviewed_record_bytes = _read_json(workspace.reviewed_record)
    ir_errors = validate_argument_ir(target_ir)
    if ir_errors:
        raise WorkbenchError("Reviewed IR is invalid: " + "; ".join(ir_errors))
    requested = [claim.upper() for claim in (claim_ids or [])]
    try:
        selected = select_review_claim_ids(target_ir, review_scope, requested)
    except ArgumentIRError as exc:
        raise WorkbenchError(str(exc)) from exc
    legacy, _, protocol_bytes = _protocol_source(lens_id)
    try:
        protocol_text = protocol_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkbenchError("Perspective Lens protocol must be UTF-8") from exc
    protocol_sha256 = sha256_bytes(protocol_bytes)

    for existing in list_perspective_reviews(workspace.root):
        record, _ = _read_json(existing.record)
        parents = {
            parent.get("role"): parent
            for parent in record.get("parents", [])
            if isinstance(parent, dict)
        }
        if (
            record.get("lens", {}).get("id") == lens_id
            and record.get("review_scope")
            == {
                "kind": review_scope,
                "claim_ids": requested,
                "selected_claim_ids": selected,
            }
            and parents.get("reviewed-ir", {}).get("sha256")
            == sha256_bytes(reviewed_record_bytes)
            and parents.get("target-ir", {}).get("sha256")
            == sha256_bytes(target_ir_bytes)
            and record.get("lens", {}).get("protocol_sha256") == protocol_sha256
        ):
            return existing, False

    review_id = _next_review_id(workspace.root)
    paths = PerspectiveReviewPaths(workspace, review_id)
    created_at = utc_now()
    protocol_record = {
        "schema_version": 1,
        "artifact": "perspective-lens-protocol",
        "artifact_id": f"{review_id}-protocol",
        "lifecycle": "immutable",
        "provenance": _provenance(
            "deterministic", created_at, "workbench-perspective-review-v1"
        ),
        "parents": [],
        "lens": {"kind": "perspective", "id": lens_id},
        "legacy_protocol": legacy,
        "protocol": {
            "relative_path": "perspective-lens.md",
            "sha256": protocol_sha256,
        },
    }
    protocol_record_bytes = json_bytes(protocol_record)
    lens = {
        "kind": "perspective",
        "id": lens_id,
        "protocol_sha256": protocol_sha256,
    }
    scope = {
        "kind": review_scope,
        "claim_ids": requested,
        "selected_claim_ids": selected,
    }
    plan = {
        "schema_version": 1,
        "artifact": "perspective-review-plan",
        "artifact_id": f"{review_id}-plan",
        "lifecycle": "immutable",
        "provenance": _provenance(
            "deterministic", created_at, "workbench-perspective-review-v1"
        ),
        "parents": [
            _parent("target-ir", "argument-ir", target_ir_bytes),
            _parent(
                "protocol", "perspective-lens-protocol", protocol_record_bytes
            ),
        ],
        "review_id": review_id,
        "lens": lens,
        "review_scope": scope,
    }
    plan_bytes = json_bytes(plan)
    prompt_bytes = render_perspective_prompt(
        plan,
        target_ir,
        protocol_text,
        plan_sha256=sha256_bytes(plan_bytes),
    ).encode("utf-8")
    record = {
        "schema_version": 1,
        "artifact": "perspective-review-run",
        "artifact_id": review_id,
        "lifecycle": "immutable",
        "provenance": _provenance(
            "deterministic", created_at, "workbench-perspective-review-v1"
        ),
        "parents": [
            _parent("reviewed-ir", "reviewed-argument-ir", reviewed_record_bytes),
            _parent("target-ir", "argument-ir", target_ir_bytes),
            _parent(
                "protocol", "perspective-lens-protocol", protocol_record_bytes
            ),
            _parent("plan", "perspective-review-plan", plan_bytes),
        ],
        "review_id": review_id,
        "project_id": reviewed_record["project_id"],
        "document_id": reviewed_record["document_id"],
        "version_id": reviewed_record["version_id"],
        "lens": lens,
        "review_scope": scope,
        "reviewed_ir_record": {
            "relative_path": "reviewed-ir-record.json",
            "sha256": sha256_bytes(reviewed_record_bytes),
        },
        "target_ir": {
            "relative_path": "target-argument-ir.json",
            "sha256": sha256_bytes(target_ir_bytes),
        },
        "protocol_record": {
            "relative_path": "perspective-lens-protocol.json",
            "sha256": sha256_bytes(protocol_record_bytes),
        },
        "protocol": {
            "relative_path": "perspective-lens.md",
            "sha256": protocol_sha256,
        },
        "plan": {
            "relative_path": "perspective-review-plan.json",
            "sha256": sha256_bytes(plan_bytes),
        },
        "prompt": {
            "relative_path": "review-prompt.md",
            "sha256": sha256_bytes(prompt_bytes),
        },
    }
    for value, label in (
        (protocol_record, "protocol"),
        (plan, "plan"),
        (record, "run"),
    ):
        errors = validate_artifact(value)
        if errors:
            raise WorkbenchError(
                f"internal Perspective Review {label} contract error: "
                + "; ".join(errors)
            )
    paths.reviews_dir.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{review_id}.", dir=paths.reviews_dir)
    )
    try:
        for relative, data in (
            ("review-run.json", json_bytes(record)),
            ("perspective-lens-protocol.json", protocol_record_bytes),
            ("perspective-lens.md", protocol_bytes),
            ("reviewed-ir-record.json", reviewed_record_bytes),
            ("target-argument-ir.json", target_ir_bytes),
            ("perspective-review-plan.json", plan_bytes),
            ("review-prompt.md", prompt_bytes),
        ):
            _write_new(temporary / relative, data)
        (temporary / "results").mkdir()
        (temporary / "derived").mkdir()
        os.replace(temporary, paths.root)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return paths, True


def _read_inputs(
    paths: PerspectiveReviewPaths,
) -> tuple[dict[str, Any], bytes, dict[str, Any], bytes, dict[str, Any], bytes]:
    review, review_bytes = _read_json(paths.record)
    plan, plan_bytes = _read_json(paths.plan)
    protocol, protocol_bytes = _read_json(paths.protocol_record)
    return review, review_bytes, plan, plan_bytes, protocol, protocol_bytes


def _classify_response(
    response_bytes: bytes,
    plan: dict[str, Any],
    plan_bytes: bytes,
) -> tuple[str, list[str], dict[str, Any] | None]:
    try:
        value = parse_json_strict(response_bytes)
    except WorkbenchError as exc:
        return "unusable", [str(exc)], None
    errors = validate_perspective_lens_results(value)
    if not isinstance(value, dict):
        return "unusable", errors, None
    expected_source = {
        "plan_sha256": sha256_bytes(plan_bytes),
        "target_ir_sha256": next(
            parent["sha256"]
            for parent in plan["parents"]
            if parent["role"] == "target-ir"
        ),
        "protocol_sha256": plan["lens"]["protocol_sha256"],
    }
    if value.get("source") != expected_source:
        errors.append("source must bind the exact plan, target IR, and protocol")
    selected = list(plan["review_scope"]["selected_claim_ids"])
    result_targets = [
        result.get("target_claim")
        for result in value.get("results", [])
        if isinstance(result, dict)
    ]
    status = value.get("status")
    if status == "complete" and result_targets != selected:
        errors.append("complete results must cover selected Claims exactly once and in order")
    if status == "partial":
        expected_subsequence = [claim for claim in selected if claim in result_targets]
        if result_targets != expected_subsequence:
            errors.append("partial results must follow selected Claim order")
    if status == "blocked" and result_targets:
        errors.append("blocked results must not contain Claim judgments")
    unknown_targets = [claim for claim in result_targets if claim not in selected]
    if unknown_targets:
        errors.append(f"results contain Claims outside the review scope: {unknown_targets}")
    return ("valid" if not errors else "unusable", errors, value)


def _validate_refs_against_ir(
    value: dict[str, Any], target_ir: dict[str, Any]
) -> list[str]:
    known = {
        str(item["id"])
        for field in ("claims", "evidence", "assumptions", "citations")
        for item in target_ir[field]
    }
    errors: list[str] = []
    for index, result in enumerate(value.get("results", [])):
        if not isinstance(result, dict):
            continue
        unknown = [ref for ref in result.get("basis_refs", []) if ref not in known]
        if unknown:
            errors.append(f"results[{index}].basis_refs contains unknown nodes: {unknown}")
    return errors


def collect_perspective_results(
    project_dir: Path | str,
    response_bytes: bytes,
    *,
    review_id: str | None,
    method: str,
    source_name: str,
    producer_label: str | None,
) -> tuple[Path, dict[str, Any]]:
    if method not in COLLECTION_METHODS:
        raise WorkbenchError("collection method must be file or terminal-paste")
    if not source_name or Path(source_name).name != source_name:
        raise WorkbenchError("collection source_name must be a basename")
    paths = selected_perspective_review(project_dir, review_id)
    workspace_errors = verify_workspace(paths.workspace)
    if workspace_errors:
        raise WorkbenchError(
            "Argument Workbench project is invalid: " + "; ".join(workspace_errors)
        )
    review, review_bytes, plan, plan_bytes, _, _ = _read_inputs(paths)
    status, errors, value = _classify_response(response_bytes, plan, plan_bytes)
    if value is not None:
        target_ir, _ = _read_json(paths.target_ir)
        errors.extend(_validate_refs_against_ir(value, target_ir))
        status = "valid" if not errors else "unusable"
    attempt_id = _next_attempt_id(paths)
    attempt_dir = paths.attempt_dir(attempt_id)
    created_at = utc_now()
    record = {
        "schema_version": 1,
        "artifact": "perspective-result-attempt",
        "artifact_id": f"{paths.review_id}-{attempt_id}",
        "lifecycle": "immutable",
        "provenance": _provenance(
            "model-derived", created_at, producer_label or "unlabeled-model"
        ),
        "parents": [
            _parent("review-run", "perspective-review-run", review_bytes)
        ],
        "review_id": paths.review_id,
        "attempt_id": attempt_id,
        "collection": {
            "method": method,
            "source_name": source_name,
            "producer_label": producer_label,
        },
        "response": {
            "relative_path": "response.json",
            "sha256": sha256_bytes(response_bytes),
        },
        "validation": {"status": status, "errors": errors},
    }
    contract_errors = validate_artifact(record)
    if contract_errors:
        raise WorkbenchError(
            "internal Perspective result contract error: "
            + "; ".join(contract_errors)
        )
    temporary = Path(
        tempfile.mkdtemp(prefix=f".{attempt_id}.", dir=paths.results_dir)
    )
    try:
        _write_new(temporary / "response.json", response_bytes)
        _write_new(temporary / "record.json", json_bytes(record))
        os.replace(temporary, attempt_dir)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    if status == "valid":
        rebuild_perspective_attempt(paths, attempt_id)
    return attempt_dir, record


def list_perspective_attempts(
    paths: PerspectiveReviewPaths,
) -> list[tuple[Path, dict[str, Any], bytes]]:
    if not paths.results_dir.exists():
        return []
    if paths.results_dir.is_symlink() or not paths.results_dir.is_dir():
        raise WorkbenchError("Perspective Review results must be a directory")
    attempts: list[tuple[Path, dict[str, Any], bytes]] = []
    for path in paths.results_dir.iterdir():
        if path.is_symlink():
            raise WorkbenchError(f"result attempt must not be a symlink: {path}")
        if path.is_dir() and ATTEMPT_ID_PATTERN.fullmatch(path.name):
            record, data = _read_json(path / "record.json")
            attempts.append((path, record, data))
    return sorted(attempts, key=lambda entry: entry[0].name)


def selected_perspective_attempt(
    paths: PerspectiveReviewPaths,
) -> tuple[Path, dict[str, Any], bytes]:
    for attempt in reversed(list_perspective_attempts(paths)):
        if attempt[1].get("validation", {}).get("status") == "valid":
            return attempt
    raise WorkbenchError(f"{paths.review_id} has no valid model result")


def _versioned(version_id: str, reference: str) -> str:
    return f"{version_id}:{reference}"


def render_perspective_review(
    review: dict[str, Any],
    target_ir: dict[str, Any],
    outcomes: list[dict[str, Any]],
    summary: dict[str, int],
    *,
    attempt_id: str,
    result_sha256: str,
    only_claim: str | None = None,
) -> str:
    claim_by_id = {
        _versioned(str(review["version_id"]), str(claim["id"])): claim
        for claim in target_ir["claims"]
    }
    selected = [
        outcome
        for outcome in outcomes
        if only_claim is None or outcome["target_claim"] == only_claim
    ]
    if only_claim is not None and only_claim not in claim_by_id:
        raise WorkbenchError(f"unknown Claim: {only_claim}")
    shown_summary = {
        verdict: sum(1 for outcome in selected if outcome["verdict"] == verdict)
        for verdict in FINDING_VERDICTS
    }
    lines = [
        "# Perspective Lens Review",
        "",
        f"- Review: `{review['review_id']}`",
        f"- Version: `{review['version_id']}`",
        f"- Lens: `{review['lens']['id']}` (Perspective Lens)",
        f"- Scope: `{review['review_scope']['kind']}`; Claims: "
        + ", ".join(review["review_scope"]["selected_claim_ids"]),
        f"- Result attempt: `{attempt_id}`",
        f"- Result SHA-256: `{result_sha256}`",
        "- All verdicts, reasons, and framework analyses are model-derived.",
        "- This lens is displayed separately; no vote or cross-lens synthesis is computed.",
        f"- Outcomes: {shown_summary['pass']} pass, {shown_summary['fail']} fail, "
        f"{shown_summary['uncertain']} uncertain",
        "",
    ]
    for outcome in selected:
        claim = claim_by_id[outcome["target_claim"]]
        marker = str(outcome["verdict"]).upper()
        finding = (
            f" `{outcome['finding_id']}`" if outcome["finding_id"] is not None else ""
        )
        lines.extend(
            [
                f"## {outcome['target_claim']} — {marker}{finding}",
                "",
                f"- Claim: {claim['text']}",
                f"- Source: {claim['source_quote']}",
                f"- Position: `{claim['position']}` `[deterministic]`",
                f"- Reason: {outcome['reason']} `[model-derived]`",
                f"- Framework analysis: {outcome['framework_analysis']} `[model-derived]`",
                f"- Basis refs: {', '.join(outcome['basis_refs'])}",
            ]
        )
        if outcome["consequence"]:
            lines.append(f"- Consequence: {outcome['consequence']}")
        lines.append("")
    return "\n".join(lines)


def _derive_attempt(
    paths: PerspectiveReviewPaths, attempt_id: str
) -> dict[str, bytes]:
    review, review_bytes, plan, plan_bytes, _, _ = _read_inputs(paths)
    attempt_dir = paths.attempt_dir(attempt_id)
    attempt, attempt_bytes = _read_json(attempt_dir / "record.json")
    response_bytes = (attempt_dir / "response.json").read_bytes()
    status, errors, results = _classify_response(response_bytes, plan, plan_bytes)
    target_ir, target_ir_bytes = _read_json(paths.target_ir)
    if results is not None:
        errors.extend(_validate_refs_against_ir(results, target_ir))
        status = "valid" if not errors else "unusable"
    if status != "valid" or results is None:
        raise WorkbenchError(
            f"cannot derive findings from invalid {attempt_id}: " + "; ".join(errors)
        )
    version_id = str(review["version_id"])
    outcomes: list[dict[str, Any]] = []
    findings: list[tuple[str, dict[str, Any], bytes]] = []
    actionable = 0
    for result in results["results"]:
        finding_id: str | None = None
        basis = [_versioned(version_id, ref) for ref in result["basis_refs"]]
        if result["verdict"] in {"fail", "uncertain"}:
            actionable += 1
            finding_id = (
                f"{version_id}-{paths.review_id}-{attempt_id}-F{actionable:04d}"
            )
            finding = {
                "schema_version": 1,
                "artifact": "argument-finding",
                "artifact_id": finding_id,
                "lifecycle": "immutable",
                "provenance": dict(attempt["provenance"]),
                "parents": [
                    _parent("target-ir", "argument-ir", target_ir_bytes),
                    _parent(
                        "lens-result", "perspective-lens-results", response_bytes
                    ),
                ],
                "finding_id": finding_id,
                "target_claim": _versioned(version_id, result["target_claim"]),
                "lens": {
                    "kind": "perspective",
                    "id": review["lens"]["id"],
                    "check_id": None,
                },
                "verdict": result["verdict"],
                "reason": result["reason"],
                "evidence_refs": basis,
                "status": "open",
            }
            finding_errors = validate_artifact(finding)
            if finding_errors:
                raise WorkbenchError(
                    "internal Perspective Finding error: "
                    + "; ".join(finding_errors)
                )
            finding_bytes = json_bytes(finding)
            findings.append(
                (f"findings/F{actionable:04d}.json", finding, finding_bytes)
            )
        outcomes.append(
            {
                "result_id": result["result_id"],
                "target_claim": _versioned(version_id, result["target_claim"]),
                "verdict": result["verdict"],
                "reason": result["reason"],
                "basis_refs": basis,
                "framework_analysis": result["framework_analysis"],
                "consequence": result["consequence"],
                "finding_id": finding_id,
            }
        )
    summary = {
        verdict: sum(1 for outcome in outcomes if outcome["verdict"] == verdict)
        for verdict in FINDING_VERDICTS
    }
    markdown = render_perspective_review(
        review,
        target_ir,
        outcomes,
        summary,
        attempt_id=attempt_id,
        result_sha256=sha256_bytes(response_bytes),
    )
    markdown_bytes = markdown.encode("utf-8")
    parents = [
        _parent("review-run", "perspective-review-run", review_bytes),
        _parent("result-attempt", "perspective-result-attempt", attempt_bytes),
        _parent("lens-result", "perspective-lens-results", response_bytes),
    ]
    for number, (_, _, data) in enumerate(findings, 1):
        parents.append(_parent(f"finding-{number:04d}", "argument-finding", data))
    index = {
        "schema_version": 1,
        "artifact": "perspective-review-index",
        "artifact_id": f"{paths.review_id}-{attempt_id}-perspective-review",
        "lifecycle": "derived-replaceable",
        "provenance": _provenance(
            "deterministic",
            str(attempt["provenance"]["created_at"]),
            "workbench-perspective-review-v1",
        ),
        "parents": parents,
        "review_id": paths.review_id,
        "attempt_id": attempt_id,
        "version_id": version_id,
        "lens": dict(review["lens"]),
        "run_status": results["status"],
        "unverified": list(results["unverified"]),
        "summary": summary,
        "outcomes": outcomes,
        "view": {
            "relative_path": "perspective-review.md",
            "sha256": sha256_bytes(markdown_bytes),
        },
        "field_provenance": {
            "outcomes": {"origin": "model-derived", "source": "lens-result"},
            "run_status": {"origin": "model-derived", "source": "lens-result"},
            "unverified": {"origin": "model-derived", "source": "lens-result"},
            "finding_id": {
                "origin": "deterministic",
                "source": "workbench-perspective-review-v1",
            },
            "summary": {
                "origin": "deterministic",
                "source": "workbench-perspective-review-v1",
            },
            "view": {
                "origin": "deterministic",
                "source": "workbench-perspective-review-v1",
            },
        },
    }
    index_errors = validate_artifact(index)
    if index_errors:
        raise WorkbenchError(
            "internal Perspective Review index error: " + "; ".join(index_errors)
        )
    files = {
        "perspective-review.md": markdown_bytes,
        "perspective-review-index.json": json_bytes(index),
    }
    for relative, _, data in findings:
        files[relative] = data
    return files


def _write_derived(root: Path, files: dict[str, bytes]) -> bool:
    if root.is_symlink():
        raise WorkbenchError(f"derived directory must not be a symlink: {root}")
    root.mkdir(parents=True, exist_ok=True)
    (root / "findings").mkdir(exist_ok=True)
    allowed = {Path(relative) for relative in files}
    changed = False
    for existing in root.rglob("*"):
        if existing.is_symlink():
            raise WorkbenchError(f"derived artifact must not be a symlink: {existing}")
        if existing.is_file() and existing.relative_to(root) not in allowed:
            relative = existing.relative_to(root)
            if relative.parent == Path("findings") and FINDING_FILE_PATTERN.fullmatch(
                relative.name
            ):
                existing.unlink()
                changed = True
            else:
                raise WorkbenchError(f"unexpected derived artifact: {relative}")
    for relative, data in files.items():
        path = root / relative
        if not path.exists() or path.read_bytes() != data:
            _atomic_write(path, data)
            changed = True
    return changed


def rebuild_perspective_attempt(
    paths: PerspectiveReviewPaths, attempt_id: str
) -> tuple[Path, bool]:
    if ATTEMPT_ID_PATTERN.fullmatch(attempt_id) is None:
        raise WorkbenchError("attempt ID must be attempt-NNNN")
    files = _derive_attempt(paths, attempt_id)
    root = paths.derived_attempt_dir(attempt_id)
    return root / "perspective-review.md", _write_derived(root, files)


def rebuild_perspective_reviews(
    project_dir: Path | str,
) -> tuple[list[Path], bool]:
    outputs: list[Path] = []
    changed = False
    for review in list_perspective_reviews(project_dir):
        for _, attempt, _ in list_perspective_attempts(review):
            attempt_id = str(attempt["attempt_id"])
            derived = review.derived_attempt_dir(attempt_id)
            if attempt.get("validation", {}).get("status") == "valid":
                output, attempt_changed = rebuild_perspective_attempt(
                    review, attempt_id
                )
                outputs.append(output)
                changed = changed or attempt_changed
            elif derived.exists():
                raise WorkbenchError(
                    f"invalid Perspective result must not have derived artifacts: {derived}"
                )
    return outputs, changed


def show_perspective_review(
    project_dir: Path | str,
    *,
    review_id: str | None,
    claim_id: str | None,
) -> tuple[str, Path]:
    paths = selected_perspective_review(project_dir, review_id)
    attempt_dir, attempt, _ = selected_perspective_attempt(paths)
    files = _derive_attempt(paths, str(attempt["attempt_id"]))
    index = parse_json_strict(files["perspective-review-index.json"])
    review, _ = _read_json(paths.record)
    target_ir, _ = _read_json(paths.target_ir)
    normalized: str | None = None
    if claim_id is not None:
        candidate = claim_id.strip().upper()
        normalized = candidate if ":" in candidate else _versioned(
            str(review["version_id"]), candidate
        )
    rendered = render_perspective_review(
        review,
        target_ir,
        list(index["outcomes"]),
        dict(index["summary"]),
        attempt_id=str(attempt["attempt_id"]),
        result_sha256=sha256_bytes((attempt_dir / "response.json").read_bytes()),
        only_claim=normalized,
    )
    view = (
        paths.derived_attempt_dir(str(attempt["attempt_id"]))
        / "perspective-review.md"
    )
    return rendered, view


def verify_perspective_reviews(project_dir: Path | str) -> list[str]:
    workspace = workspace_paths(project_dir)
    root = workspace.version_dir / "perspective-reviews"
    if not root.exists():
        return []
    errors: list[str] = []
    try:
        reviews = list_perspective_reviews(project_dir)
    except (OSError, WorkbenchError) as exc:
        return [str(exc)]
    if {path.name for path in root.iterdir()} != {review.review_id for review in reviews}:
        errors.append("perspective-reviews contains unexpected entries")
    numbers = [
        int(PERSPECTIVE_REVIEW_ID_PATTERN.fullmatch(review.review_id).group(1))
        for review in reviews
    ]
    if numbers != list(range(1, len(reviews) + 1)):
        errors.append("Perspective Review IDs must be continuous from PV1")
    base_entries: list[tuple[object, bytes]] = []
    try:
        for path in (workspace.project, workspace.document, workspace.version):
            base_entries.append(_read_json(path))
        base_entries.extend((value, data) for _, value, data in list_attempts(workspace))
        base_entries.extend(
            (value, data) for _, value, data in correction_entries(workspace)
        )
    except (OSError, WorkbenchError) as exc:
        return [f"cannot load provenance ancestors: {exc}"]
    for paths in reviews:
        prefix = paths.review_id
        try:
            (
                review,
                review_bytes,
                plan,
                plan_bytes,
                protocol,
                protocol_record_bytes,
            ) = _read_inputs(paths)
            reviewed_record, reviewed_record_bytes = _read_json(paths.reviewed_record)
            target_ir, target_ir_bytes = _read_json(paths.target_ir)
        except (OSError, WorkbenchError) as exc:
            errors.append(f"{prefix}: {exc}")
            continue
        entries = list(base_entries)
        entries.extend(
            [
                (reviewed_record, reviewed_record_bytes),
                (protocol, protocol_record_bytes),
                (plan, plan_bytes),
                (review, review_bytes),
            ]
        )
        for label, value in (
            ("protocol", protocol),
            ("plan", plan),
            ("run", review),
        ):
            errors.extend(
                f"{prefix}: {label}: {error}"
                for error in validate_artifact(value)
            )
        if validate_argument_ir(target_ir):
            errors.append(f"{prefix}: target IR snapshot is invalid")
        if reviewed_record.get("payload", {}).get("sha256") != sha256_bytes(
            target_ir_bytes
        ):
            errors.append(f"{prefix}: Reviewed IR payload hash is disconnected")
        protocol_parents = {
            parent.get("role"): parent
            for parent in protocol.get("parents", [])
            if isinstance(parent, dict)
        }
        plan_parents = {
            parent.get("role"): parent
            for parent in plan.get("parents", [])
            if isinstance(parent, dict)
        }
        review_parents = {
            parent.get("role"): parent
            for parent in review.get("parents", [])
            if isinstance(parent, dict)
        }
        if protocol_parents:
            errors.append(f"{prefix}: protocol snapshot must not have parents")
        expected_plan_parents = {
            "target-ir": sha256_bytes(target_ir_bytes),
            "protocol": sha256_bytes(protocol_record_bytes),
        }
        expected_review_parents = {
            "reviewed-ir": sha256_bytes(reviewed_record_bytes),
            "target-ir": sha256_bytes(target_ir_bytes),
            "protocol": sha256_bytes(protocol_record_bytes),
            "plan": sha256_bytes(plan_bytes),
        }
        for role, digest in expected_plan_parents.items():
            if plan_parents.get(role, {}).get("sha256") != digest:
                errors.append(f"{prefix}: plan {role} parent is disconnected")
        for role, digest in expected_review_parents.items():
            if review_parents.get(role, {}).get("sha256") != digest:
                errors.append(f"{prefix}: run {role} parent is disconnected")
        protocol_lens = protocol.get("lens")
        plan_lens = plan.get("lens")
        review_lens = review.get("lens")
        if not all(
            isinstance(value, dict)
            for value in (protocol_lens, plan_lens, review_lens)
        ) or not (
            protocol_lens.get("id")
            == plan_lens.get("id")
            == review_lens.get("id")
        ):
            errors.append(f"{prefix}: Perspective Lens identity is inconsistent")
        if plan_lens != review_lens:
            errors.append(f"{prefix}: plan and run Lens bindings differ")
        for field in ("project_id", "document_id", "version_id"):
            if review.get(field) != reviewed_record.get(field):
                errors.append(
                    f"{prefix}: {field} does not match Reviewed IR snapshot"
                )
        bindings = {
            "reviewed_ir_record": paths.reviewed_record,
            "target_ir": paths.target_ir,
            "protocol_record": paths.protocol_record,
            "protocol": paths.protocol,
            "plan": paths.plan,
            "prompt": paths.prompt,
        }
        for field, path in bindings.items():
            if path.is_symlink() or not path.is_file():
                errors.append(f"{prefix}: {field} is missing or unsafe")
                continue
            if review.get(field) != {
                "relative_path": path.name,
                "sha256": sha256_bytes(path.read_bytes()),
            }:
                errors.append(f"{prefix}: {field} binding does not match exact bytes")
        try:
            stored_scope = plan.get("review_scope")
            if not isinstance(stored_scope, dict):
                raise TypeError("review_scope must be an object")
            expected_scope = {
                "kind": stored_scope.get("kind"),
                "claim_ids": stored_scope.get("claim_ids", []),
                "selected_claim_ids": select_review_claim_ids(
                    target_ir,
                    str(stored_scope.get("kind")),
                    list(stored_scope.get("claim_ids", [])),
                ),
            }
        except (ArgumentIRError, TypeError) as exc:
            errors.append(f"{prefix}: cannot reproduce review scope: {exc}")
        else:
            if (
                plan.get("review_scope") != expected_scope
                or review.get("review_scope") != expected_scope
            ):
                errors.append(f"{prefix}: review scope is not reproducible")
        try:
            expected_prompt = render_perspective_prompt(
                plan,
                target_ir,
                paths.protocol.read_bytes().decode("utf-8"),
                plan_sha256=sha256_bytes(plan_bytes),
            ).encode("utf-8")
            if paths.prompt.read_bytes() != expected_prompt:
                errors.append(f"{prefix}: review prompt is not reproducible")
        except (OSError, UnicodeDecodeError, ArgumentIRError) as exc:
            errors.append(f"{prefix}: cannot reproduce prompt: {exc}")
        try:
            attempts = list_perspective_attempts(paths)
        except (OSError, WorkbenchError) as exc:
            errors.append(f"{prefix}: {exc}")
            continue
        known = {path.name for path, _, _ in attempts}
        if {path.name for path in paths.results_dir.iterdir()} != known:
            errors.append(f"{prefix}: results contains unexpected entries")
        if paths.derived_dir.is_symlink() or not paths.derived_dir.is_dir():
            errors.append(f"{prefix}: derived must be a regular directory")
            continue
        for child in paths.derived_dir.iterdir():
            if child.name not in known:
                errors.append(f"{prefix}: derived contains unexpected entry: {child.name}")
        for attempt_dir, attempt, attempt_bytes in attempts:
            attempt_prefix = f"{prefix}/{attempt_dir.name}"
            entries.append((attempt, attempt_bytes))
            errors.extend(
                f"{attempt_prefix}: {error}" for error in validate_artifact(attempt)
            )
            response_path = attempt_dir / "response.json"
            if response_path.is_symlink() or not response_path.is_file():
                errors.append(f"{attempt_prefix}: response is missing or unsafe")
                continue
            response_bytes = response_path.read_bytes()
            status, result_errors, result = _classify_response(
                response_bytes, plan, plan_bytes
            )
            if result is not None:
                result_errors.extend(_validate_refs_against_ir(result, target_ir))
                status = "valid" if not result_errors else "unusable"
            expected_validation = {"status": status, "errors": result_errors}
            if attempt.get("validation") != expected_validation:
                errors.append(f"{attempt_prefix}: validation is not reproducible")
            derived = paths.derived_attempt_dir(attempt_dir.name)
            if status == "valid":
                entries.append((result, response_bytes))
                expected_files = _derive_attempt(paths, attempt_dir.name)
                actual = {
                    path.relative_to(derived).as_posix(): path.read_bytes()
                    for path in derived.rglob("*")
                    if path.is_file() and not path.is_symlink()
                } if derived.is_dir() and not derived.is_symlink() else {}
                if actual != expected_files:
                    errors.append(f"{attempt_prefix}: derived cache is not reproducible")
                for relative, data in expected_files.items():
                    if relative.endswith(".json"):
                        entries.append((parse_json_strict(data), data))
            elif derived.exists() or derived.is_symlink():
                errors.append(f"{attempt_prefix}: invalid result has derived artifacts")
        errors.extend(f"{prefix}: {error}" for error in validate_contract_bundle(entries))
    return errors
