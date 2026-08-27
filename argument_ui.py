"""Local, document-first application layer for Argument Workbench.

The core engine does not import this module.  The UI reads validated artifacts
and delegates every mutation to an existing domain service, so opening the UI
cannot turn model output into human-confirmed state.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import webbrowser
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from argument_adjudication import (
    append_finding_decision,
    current_finding_entries,
    human_review_paths,
    latest_adjudications,
    list_adjudications,
    list_revision_actions,
)
from argument_citations import list_citation_audits
from argument_contracts import sha256_bytes
from argument_perspective import list_perspective_reviews, selected_perspective_attempt
from argument_resolution import list_resolutions
from argument_review import list_rule_reviews, selected_result_attempt
from argument_workbench import (
    WorkbenchError,
    _read_json,
    list_version_ids,
    parse_json_strict,
    project_mutation_lock,
    verify_project_versions,
    workspace_paths,
)


MAX_REQUEST_BYTES = 1024 * 1024
LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}
POSITION_PATTERN = re.compile(
    r"L(?P<start_line>[1-9][0-9]*):C(?P<start_column>[1-9][0-9]*)"
    r"-L(?P<end_line>[1-9][0-9]*):C(?P<end_column>[1-9][0-9]*)\Z"
)


def _source(workspace) -> tuple[str, dict[str, Any]]:
    version, _ = _read_json(workspace.version)
    relative = version.get("source", {}).get("relative_path")
    if not isinstance(relative, str):
        raise WorkbenchError("DocumentVersion source path is invalid")
    path = workspace.version_dir / Path(relative)
    if path.is_symlink() or not path.is_file():
        raise WorkbenchError("DocumentVersion source must be a regular file")
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise WorkbenchError(f"DocumentVersion source is not UTF-8: {exc}") from exc
    return text, version


def _node_table(ir: dict[str, Any]) -> dict[str, dict[str, Any]]:
    table: dict[str, dict[str, Any]] = {}
    for collection in ("claims", "evidence", "assumptions", "citations"):
        for node in ir.get(collection, []):
            if isinstance(node, dict) and isinstance(node.get("id"), str):
                table[str(node["id"])] = {**node, "node_kind": collection[:-1]}
    return table


def _manuscript_lines(text: str, claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_line: dict[int, list[str]] = {}
    for claim in claims:
        position = str(claim.get("position", ""))
        match = POSITION_PATTERN.fullmatch(position)
        if match is not None:
            for number in range(
                int(match.group("start_line")), int(match.group("end_line")) + 1
            ):
                by_line.setdefault(number, []).append(str(claim["id"]))
            continue
        quote = str(claim.get("source_quote", ""))
        if not quote:
            continue
        offset = text.find(quote)
        if offset < 0:
            continue
        start = text.count("\n", 0, offset) + 1
        end = start + quote.count("\n")
        for number in range(start, end + 1):
            by_line.setdefault(number, []).append(str(claim["id"]))
    return [
        {"number": number, "text": line, "claim_ids": by_line.get(number, [])}
        for number, line in enumerate(text.splitlines(), 1)
    ]


def _review_outcomes(workspace) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    outcomes: list[dict[str, Any]] = []
    lenses: list[dict[str, Any]] = []
    current_ir_sha256 = sha256_bytes(workspace.reviewed_payload.read_bytes())
    for review in list_rule_reviews(workspace):
        record, _ = _read_json(review.record)
        target_parent = next(
            (
                parent
                for parent in record.get("parents", [])
                if isinstance(parent, dict) and parent.get("role") == "target-ir"
            ),
            None,
        )
        if target_parent is None or target_parent.get("sha256") != current_ir_sha256:
            continue
        try:
            attempt_dir, _, _ = selected_result_attempt(review)
        except WorkbenchError:
            continue
        index, _ = _read_json(
            review.derived_attempt_dir(attempt_dir.name) / "claim-review-index.json"
        )
        library, _ = _read_json(review.library)
        check_by_id = {
            str(check["id"]): check
            for check in library.get("checks", [])
            if isinstance(check, dict) and isinstance(check.get("id"), str)
        }
        lens = index.get("lens", {})
        lenses.append(
            {
                "review_id": review.review_id,
                "kind": "rule",
                "id": str(lens.get("id", "Rule Lens")),
                "protocol_text": "",
            }
        )
        for outcome in index.get("outcomes", []):
            if isinstance(outcome, dict):
                check = check_by_id.get(str(outcome.get("check_id")), {})
                outcomes.append(
                    {
                        **outcome,
                        "review_id": review.review_id,
                        "lens": {
                            "kind": "rule",
                            "id": str(lens.get("id", "Rule Lens")),
                            "check_id": outcome.get("check_id"),
                        },
                        "lens_basis": {
                            "label": check.get("label", outcome.get("check_id")),
                            "question": check.get("question", ""),
                            "failure_condition": check.get("failure_condition", ""),
                            "evidence_policy": check.get("evidence_policy", ""),
                        },
                    }
                )
    for review in list_perspective_reviews(workspace):
        record, _ = _read_json(review.record)
        target_parent = next(
            (
                parent
                for parent in record.get("parents", [])
                if isinstance(parent, dict) and parent.get("role") == "target-ir"
            ),
            None,
        )
        if target_parent is None or target_parent.get("sha256") != current_ir_sha256:
            continue
        try:
            attempt_dir, _, _ = selected_perspective_attempt(review)
        except WorkbenchError:
            continue
        index, _ = _read_json(
            review.derived_attempt_dir(attempt_dir.name)
            / "perspective-review-index.json"
        )
        lens = index.get("lens", {})
        protocol_text = review.protocol.read_text(encoding="utf-8")
        lenses.append(
            {
                "review_id": review.review_id,
                "kind": "perspective",
                "id": str(lens.get("id", "Perspective Lens")),
                "protocol_text": protocol_text,
            }
        )
        for outcome in index.get("outcomes", []):
            if isinstance(outcome, dict):
                outcomes.append(
                    {
                        **outcome,
                        "review_id": review.review_id,
                        "lens": {
                            "kind": "perspective",
                            "id": str(lens.get("id", "Perspective Lens")),
                            "check_id": None,
                        },
                        "lens_basis": {
                            "label": str(lens.get("id", "Perspective Lens")),
                            "question": "Holistic application of the complete Perspective Lens protocol.",
                            "failure_condition": "See the preserved framework analysis and complete protocol.",
                            "evidence_policy": "framework-commitment",
                        },
                    }
                )
    return outcomes, lenses


def _findings(workspace) -> tuple[list[dict[str, Any]], dict[str, int]]:
    paths = human_review_paths(workspace)
    latest = latest_adjudications(list_adjudications(paths))
    action_entries = list_revision_actions(paths)
    actions_by_adjudication: dict[str, list[dict[str, Any]]] = {}
    for _, action, action_bytes in action_entries:
        actions_by_adjudication.setdefault(str(action["adjudication_id"]), []).append(
            {
                "action_id": action["action_id"],
                "action_type": action["action_type"],
                "text": action["text"],
                "sha256": sha256_bytes(action_bytes),
            }
        )
    try:
        entries = current_finding_entries(workspace)
    except WorkbenchError as exc:
        if "no current Review Lens" not in str(exc):
            raise
        entries = []
    rows: list[dict[str, Any]] = []
    counts = {"open": 0, "accept": 0, "reject": 0, "defer": 0}
    for entry in entries:
        finding_id = str(entry.value["finding_id"])
        adjudication_entry = latest.get(finding_id)
        adjudication = adjudication_entry[1] if adjudication_entry is not None else None
        adjudication_bytes = adjudication_entry[2] if adjudication_entry is not None else None
        decision = str(adjudication["decision"]) if adjudication is not None else "open"
        counts[decision] += 1
        version, _ = _read_json(workspace.version)
        response_path = entry.review.results_dir / entry.attempt_id / "response.json"
        lens_path = (
            entry.review.library
            if hasattr(entry.review, "library")
            else entry.review.protocol
        )
        parent_by_role = {
            str(parent.get("role")): parent
            for parent in entry.value.get("parents", [])
            if isinstance(parent, dict)
        }
        rows.append(
            {
                **entry.value,
                "decision": None if adjudication is None else decision,
                "human_reason": "" if adjudication is None else str(adjudication["reason"]),
                "adjudication_id": None
                if adjudication is None
                else str(adjudication["adjudication_id"]),
                "actions": []
                if adjudication is None
                else actions_by_adjudication.get(str(adjudication["adjudication_id"]), []),
                "provenance_trace": {
                    "source_sha256": version["source"]["sha256"],
                    "reviewed_ir_sha256": parent_by_role.get("target-ir", {}).get("sha256"),
                    "review_run_sha256": sha256_bytes(entry.review.record.read_bytes()),
                    "lens_protocol_sha256": sha256_bytes(lens_path.read_bytes()),
                    "model_result_sha256": sha256_bytes(response_path.read_bytes()),
                    "finding_sha256": sha256_bytes(entry.data),
                    "adjudication_sha256": None
                    if adjudication_bytes is None
                    else sha256_bytes(adjudication_bytes),
                    "action_sha256s": []
                    if adjudication is None
                    else [
                        item["sha256"]
                        for item in actions_by_adjudication.get(
                            str(adjudication["adjudication_id"]), []
                        )
                    ],
                },
            }
        )
    return rows, counts


def _citation_state(workspace, ir: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    citation_by_id = {
        str(item["id"]): item
        for item in ir.get("citations", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    current: dict[str, dict[str, Any]] = {}
    for audit in list_citation_audits(
        workspace.root, version_id=workspace.version_id
    ):
        if (
            not audit.reviewed_ir.is_file()
            or audit.reviewed_ir.is_symlink()
            or audit.reviewed_ir.read_bytes() != workspace.reviewed_payload.read_bytes()
        ):
            continue
        if audit.index.is_file() and not audit.index.is_symlink():
            index, _ = _read_json(audit.index)
            dependencies: dict[str, list[str]] = {}
            for dependency in index.get("claim_dependencies", []):
                if not isinstance(dependency, dict):
                    continue
                for citation_id in dependency.get("citation_ids", []):
                    dependencies.setdefault(str(citation_id), []).append(
                        str(dependency["node_id"])
                    )
            for row in index.get("citations", []):
                if isinstance(row, dict):
                    citation_id = str(row["citation_id"])
                    current[citation_id] = {
                        **row,
                        "audit_id": audit.audit_id,
                        "dependent_claims": dependencies.get(citation_id, []),
                    }
    rows: list[dict[str, Any]] = []
    for citation_id, citation in citation_by_id.items():
        state = current.get(citation_id)
        rows.append(
            {
                **citation,
                "audit_id": None if state is None else state["audit_id"],
                "verification_state": "unverified"
                if state is None
                else state["verification_state"],
                "human_decision": None if state is None else state["human_decision"],
                "dependent_claims": [] if state is None else state["dependent_claims"],
            }
        )
    unverified = sum(1 for row in rows if row["verification_state"] != "verified")
    return rows, unverified


def _lineage_history(root: Path) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    base = workspace_paths(root).document_dir / "lineage"
    if not base.exists():
        return history
    if base.is_symlink() or not base.is_dir():
        raise WorkbenchError("lineage must be a regular directory")
    for pair in sorted(base.iterdir()):
        if pair.is_symlink() or not pair.is_dir() or re.fullmatch(
            r"V[1-9][0-9]*--V[1-9][0-9]*", pair.name
        ) is None:
            raise WorkbenchError(f"unexpected lineage entry: {pair.name}")
        analyses = pair / "analyses"
        if not analyses.is_dir() or analyses.is_symlink():
            continue
        candidates = sorted(
            (item for item in analyses.iterdir() if item.is_dir() and not item.is_symlink()),
            key=lambda item: int(item.name[2:]) if re.fullmatch(r"LA[1-9][0-9]*", item.name) else -1,
        )
        if not candidates:
            continue
        selected = candidates[-1]
        derived = selected / "derived"
        attempts = sorted(
            item for item in derived.iterdir() if item.is_dir() and not item.is_symlink()
        ) if derived.is_dir() and not derived.is_symlink() else []
        if not attempts:
            continue
        index_path = attempts[-1] / "claim-lineage-index.json"
        if not index_path.is_file() or index_path.is_symlink():
            continue
        index, _ = _read_json(index_path)
        decisions: dict[str, dict[str, Any]] = {}
        decisions_dir = selected / "human-decisions"
        if decisions_dir.is_dir() and not decisions_dir.is_symlink():
            for decision_path in sorted(decisions_dir.glob("LD[0-9][0-9][0-9][0-9].json")):
                decision, _ = _read_json(decision_path)
                proposal_sha256 = str(decision.get("proposal_sha256", ""))
                decisions[proposal_sha256] = {
                    **decision,
                    "decision": decision.get("review_action"),
                }
        proposals: list[dict[str, Any]] = []
        for number, proposal in enumerate(index.get("proposals", []), 1):
            if not isinstance(proposal, dict):
                continue
            lineage_path = attempts[-1] / "lineages" / f"L{number:04d}.json"
            lineage_hash = (
                sha256_bytes(lineage_path.read_bytes())
                if lineage_path.is_file() and not lineage_path.is_symlink()
                else ""
            )
            proposals.append(
                {**proposal, "human_decision": decisions.get(lineage_hash)}
            )
        history.append(
            {
                "pair": pair.name,
                "analysis_id": selected.name,
                "summary": index.get("summary", {}),
                "proposals": proposals,
            }
        )
    return history


def _resolution_history(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for paths in list_resolutions(root):
        run, _ = _read_json(paths.record)
        original_finding, _ = _read_json(paths.root / run["original_finding"]["relative_path"])
        accepted_adjudication, _ = _read_json(
            paths.root / run["accepted_adjudication"]["relative_path"]
        )
        revision_actions: list[dict[str, Any]] = []
        for action_ref in run.get("revision_actions", []):
            action, _ = _read_json(paths.root / action_ref["relative_path"])
            revision_actions.append(
                {
                    "action_id": action["action_id"],
                    "action_type": action["action_type"],
                    "text": action["text"],
                }
            )
        proposal: dict[str, Any] | None = None
        derived = paths.root / "derived"
        candidates = sorted(derived.glob("attempt-*/resolution-proposal.json")) if derived.is_dir() else []
        if candidates:
            proposal, _ = _read_json(candidates[-1])
        elif (paths.root / "derived" / "obsolete-proposal.json").is_file():
            proposal, _ = _read_json(paths.root / "derived" / "obsolete-proposal.json")
        decisions: list[dict[str, Any]] = []
        if paths.decisions_dir.is_dir() and not paths.decisions_dir.is_symlink():
            for decision_path in sorted(paths.decisions_dir.glob("RD[0-9][0-9][0-9][0-9].json")):
                decision, _ = _read_json(decision_path)
                decisions.append(decision)
        rows.append(
            {
                "resolution_id": paths.resolution_id,
                "from_version": paths.from_version,
                "to_version": paths.to_version,
                "original_finding_id": run["original_finding_id"],
                "descendant_claims": run["descendant_claims"],
                "lens": run["lens"],
                "original_finding": {
                    "target_claim": original_finding["target_claim"],
                    "verdict": original_finding["verdict"],
                    "reason": original_finding["reason"],
                },
                "accepted_reason": accepted_adjudication["reason"],
                "revision_actions": revision_actions,
                "proposed_status": None if proposal is None else proposal.get("proposed_status"),
                "human_decision": decisions[-1] if decisions else None,
            }
        )
    return rows


def _version_summaries(root: Path, versions: list[str]) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    for version_id in versions:
        workspace = workspace_paths(root, version_id)
        version, _ = _read_json(workspace.version)
        ir, _ = _read_json(workspace.reviewed_payload)
        _, counts = _findings(workspace)
        _, unverified_citations = _citation_state(workspace, ir)
        correction_count = (
            len(list(workspace.corrections_dir.glob("IC[0-9][0-9][0-9][0-9].json")))
            if workspace.corrections_dir.is_dir()
            else 0
        )
        summaries.append(
            {
                "version_id": version_id,
                "source_name": version["source"]["name"],
                "source_sha256": version["source"]["sha256"],
                "parent_version": version.get("parent_version"),
                "claims": len(ir.get("claims", [])),
                "corrections": correction_count,
                "findings": counts,
                "unverified_citations": unverified_citations,
            }
        )
    return summaries


def build_project_view(project_dir: Path | str, version_id: str | None = None) -> dict[str, Any]:
    """Return a validated, JSON-safe document-first projection of one project."""
    root = workspace_paths(project_dir).root
    errors = verify_project_versions(root)
    if errors:
        raise WorkbenchError("Argument Workbench project is invalid: " + "; ".join(errors))
    versions = list_version_ids(root)
    if not versions:
        raise WorkbenchError("project has no DocumentVersion")
    selected = version_id.upper() if version_id else versions[-1]
    if selected not in versions:
        raise WorkbenchError(f"unknown DocumentVersion: {selected}")
    workspace = workspace_paths(root, selected)
    if not workspace.reviewed_payload.is_file() or workspace.reviewed_payload.is_symlink():
        raise WorkbenchError(f"{selected} has no Reviewed Argument IR")
    ir, _ = _read_json(workspace.reviewed_payload)
    source_text, version = _source(workspace)
    project, _ = _read_json(workspace.project)
    document, _ = _read_json(workspace.document)
    nodes = _node_table(ir)
    relations = [item for item in ir.get("relations", []) if isinstance(item, dict)]
    incoming: dict[str, list[dict[str, Any]]] = {}
    outgoing: dict[str, list[dict[str, Any]]] = {}
    for relation in relations:
        incoming.setdefault(str(relation.get("to")), []).append(relation)
        outgoing.setdefault(str(relation.get("from")), []).append(relation)
    findings, finding_counts = _findings(workspace)
    outcomes, lenses = _review_outcomes(workspace)
    citations, unverified_citations = _citation_state(workspace, ir)
    claim_findings: dict[str, list[str]] = {}
    for finding in findings:
        claim_findings.setdefault(str(finding["target_claim"]).split(":", 1)[-1], []).append(
            str(finding["finding_id"])
        )
    claims: list[dict[str, Any]] = []
    for claim in ir.get("claims", []):
        if not isinstance(claim, dict):
            continue
        claim_id = str(claim["id"])
        claims.append(
            {
                **claim,
                "versioned_id": f"{selected}:{claim_id}",
                "incoming": incoming.get(claim_id, []),
                "outgoing": outgoing.get(claim_id, []),
                "finding_ids": claim_findings.get(claim_id, []),
            }
        )
    resolution_history = _resolution_history(root)
    resolved = sum(
        1
        for row in resolution_history
        if (row.get("human_decision") or {}).get("final_status") == "resolved"
    )
    return {
        "project": {
            "project_id": project["project_id"],
            "title": project["title"],
            "document_id": document["document_id"],
            "version_id": selected,
            "current_version": versions[-1],
            "versions": versions,
            "source_name": version["source"]["name"],
        },
        "dashboard": {
            "claims": len(claims),
            "open_findings": finding_counts["open"],
            "accepted": finding_counts["accept"],
            "rejected": finding_counts["reject"],
            "deferred": finding_counts["defer"],
            "resolved": resolved,
            "unverified_citations": unverified_citations,
        },
        "manuscript": _manuscript_lines(source_text, claims),
        "version_history": _version_summaries(root, versions),
        "claims": claims,
        "nodes": nodes,
        "relations": relations,
        "lenses": lenses,
        "outcomes": outcomes,
        "findings": findings,
        "citations": citations,
        "lineage": _lineage_history(root),
        "resolutions": resolution_history,
        "permissions": {"can_adjudicate": selected == versions[-1]},
        "provenance_legend": {
            "source_position": "deterministic",
            "claim_semantics": "model-derived or human-corrected",
            "review_outcomes": "model-derived",
            "adjudications": "human-confirmed",
            "relations": "model-derived or human-corrected",
        },
    }


def adjudicate_from_ui(project_dir: Path | str, payload: dict[str, Any]) -> None:
    allowed = {"finding_id", "decision", "reason", "actions"}
    if set(payload) != allowed:
        raise WorkbenchError("adjudication request has unexpected or missing fields")
    finding_id = payload.get("finding_id")
    decision = payload.get("decision")
    reason = payload.get("reason")
    raw_actions = payload.get("actions")
    if not all(isinstance(value, str) for value in (finding_id, decision, reason)):
        raise WorkbenchError("finding_id, decision, and reason must be text")
    if not isinstance(raw_actions, list):
        raise WorkbenchError("actions must be a list")
    actions: list[tuple[str, str]] = []
    for action in raw_actions:
        if not isinstance(action, dict) or set(action) != {"action_type", "text"}:
            raise WorkbenchError("each action needs only action_type and text")
        if not isinstance(action["action_type"], str) or not isinstance(action["text"], str):
            raise WorkbenchError("revision action fields must be text")
        actions.append((action["action_type"], action["text"]))
    append_finding_decision(
        project_dir,
        finding_id,
        decision=decision,
        reason=reason,
        actions=actions,
        producer="local-workbench-ui",
    )


@dataclass(frozen=True)
class LocalWorkbench:
    project_dir: Path
    token: str

    @classmethod
    def create(cls, project_dir: Path | str) -> "LocalWorkbench":
        root = workspace_paths(project_dir).root
        build_project_view(root)
        return cls(root, secrets.token_urlsafe(32))

    def view(self, version_id: str | None = None) -> dict[str, Any]:
        return build_project_view(self.project_dir, version_id)

    def adjudicate(self, payload: dict[str, Any]) -> dict[str, Any]:
        adjudicate_from_ui(self.project_dir, payload)
        return self.view()


class WorkbenchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: LocalWorkbench):
        self.app = app
        self.action_lock = threading.RLock()
        super().__init__(address, WorkbenchRequestHandler)


class WorkbenchRequestHandler(BaseHTTPRequestHandler):
    server: WorkbenchHTTPServer

    def log_message(self, format: str, *args: object) -> None:
        return

    def _headers(self, status: HTTPStatus, content_type: str, length: int) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self._headers(status, content_type, len(body))
        self.wfile.write(body)

    def _json(self, status: HTTPStatus, value: Any) -> None:
        self._send(
            status,
            (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _authorized(self) -> bool:
        return secrets.compare_digest(
            self.headers.get("X-Argument-Workbench-Token", ""), self.server.app.token
        )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            body = render_app_shell(self.server.app.token).encode("utf-8")
            self._send(HTTPStatus.OK, body, "text/html; charset=utf-8")
            return
        if parsed.path == "/api/view":
            if not self._authorized():
                self._json(HTTPStatus.FORBIDDEN, {"error": "local UI token required"})
                return
            version = parse_qs(parsed.query).get("version", [None])[0]
            try:
                with self.server.action_lock:
                    result = self.server.app.view(version)
                self._json(HTTPStatus.OK, result)
            except WorkbenchError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except Exception:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "本地服务处理请求时发生未预期错误，请刷新页面并检查项目状态"})
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != "/api/adjudications":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            self._json(HTTPStatus.FORBIDDEN, {"error": "local UI token required"})
            return
        if self.headers.get_content_type() != "application/json":
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "JSON required"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "invalid request size"})
            return
        try:
            payload = parse_json_strict(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise WorkbenchError("request body must be an object")
            with self.server.action_lock, project_mutation_lock(self.server.app.project_dir):
                result = self.server.app.adjudicate(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, WorkbenchError) as exc:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except Exception:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "本地服务处理请求时发生未预期错误；操作可能已经完成，请刷新页面并检查项目状态后再决定是否重试"})
            return
        self._json(HTTPStatus.CREATED, result)


def serve_workbench(
    project_dir: Path | str,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    open_browser: bool = True,
) -> tuple[WorkbenchHTTPServer, str]:
    if host.casefold() not in LOOPBACK_HOSTS:
        raise WorkbenchError("Local Workbench UI may only listen on a loopback address")
    if not 0 <= port <= 65535:
        raise WorkbenchError("port must be between 0 and 65535")
    app = LocalWorkbench.create(project_dir)
    server = WorkbenchHTTPServer((host, port), app)
    address = server.server_address
    display_host = "[::1]" if ":" in str(address[0]) else str(address[0])
    url = f"http://{display_host}:{address[1]}/"
    if open_browser:
        threading.Timer(0.25, lambda: webbrowser.open(url)).start()
    return server, url


def render_app_shell(token: str) -> str:
    token_json = json.dumps(token)
    return APP_SHELL.replace("__WORKBENCH_TOKEN__", token_json)


APP_SHELL = r'''<!doctype html>
<html lang="zh-Hans">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Argument Workbench</title>
<style>
:root{color-scheme:light;--ink:#17211c;--muted:#66736c;--paper:#f7f5ef;--panel:#fffefa;--line:#d9ddd6;--green:#1d5d45;--red:#a33c35;--amber:#9a6517;--blue:#275d8c}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
button,select,input,textarea{font:inherit}.top{position:sticky;top:0;z-index:4;background:#18392e;color:white;padding:12px 18px;box-shadow:0 2px 12px #0002}.topline{display:flex;gap:16px;align-items:center;justify-content:space-between}.brand{font-weight:700;letter-spacing:.02em}.version{display:flex;align-items:center;gap:8px}.metrics{display:grid;grid-template-columns:repeat(6,minmax(80px,1fr));gap:8px;margin-top:10px}.metric{background:#ffffff14;padding:8px 10px;border-radius:8px}.metric b{display:block;font-size:19px}.metric span{font-size:11px;opacity:.8}
.workspace{display:grid;grid-template-columns:minmax(360px,1.25fr) minmax(300px,.9fr) minmax(360px,1fr);height:calc(100vh - 126px)}.pane{overflow:auto;border-right:1px solid var(--line);background:var(--panel)}.pane:last-child{border:0}.pane-head{position:sticky;top:0;background:#fffefaeF;backdrop-filter:blur(8px);padding:14px 16px 10px;border-bottom:1px solid var(--line);z-index:2}.pane-head h2{font-size:14px;text-transform:uppercase;letter-spacing:.09em;margin:0}.pane-body{padding:12px 16px 60px}
.line{display:grid;grid-template-columns:42px 1fr;gap:10px;padding:2px 6px;border-radius:5px;white-space:pre-wrap}.line:hover{background:#edf3ef}.line.active{background:#dbeae2}.ln{color:#9aa29d;text-align:right;user-select:none}.claim-chip,.badge{display:inline-flex;border:1px solid var(--line);border-radius:999px;padding:1px 7px;font-size:11px;margin-left:6px;background:white;cursor:pointer}.claim-list button{width:100%;text-align:left;border:1px solid var(--line);background:white;padding:10px;margin:0 0 8px;border-radius:9px}.claim-list button.active{border-color:var(--green);box-shadow:0 0 0 2px #1d5d4522}.claim-id{font-weight:700;color:var(--green)}.muted{color:var(--muted)}.section{margin:18px 0}.section h3{font-size:12px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:0 0 8px}.card{border:1px solid var(--line);background:white;border-radius:10px;padding:11px 12px;margin:0 0 9px}.verdict-fail{border-left:4px solid var(--red)}.verdict-uncertain{border-left:4px solid var(--amber)}.verdict-pass{border-left:4px solid var(--green)}.status{font-size:11px;font-weight:700;text-transform:uppercase}.human{color:var(--blue)}.model{color:var(--amber)}.deterministic{color:var(--green)}
.relation{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:12px;word-break:break-all}.decision{display:flex;gap:6px;margin-top:10px}.decision button,.history-button{border:1px solid var(--line);background:#f6f7f4;border-radius:7px;padding:5px 9px;cursor:pointer}.decision button:hover{border-color:var(--green)}.history-button{background:#ffffff18;color:white;border-color:#ffffff55}details{margin-top:9px}summary{cursor:pointer;font-weight:600}pre.protocol{white-space:pre-wrap;max-height:280px;overflow:auto;background:#f3f4f0;padding:9px;border-radius:7px;font-size:12px}dialog{border:0;border-radius:12px;box-shadow:0 18px 70px #0005;max-width:720px;width:calc(100% - 32px)}dialog::backdrop{background:#10251c88}label{display:block;margin:10px 0 4px;font-weight:600}textarea,input,select{width:100%;border:1px solid var(--line);border-radius:7px;padding:8px}.dialog-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:14px}.primary{background:var(--green)!important;color:white;border-color:var(--green)!important}.error{background:#ffe8e5;color:#7b241f;padding:9px;border-radius:7px;margin:9px 0}.empty{padding:24px;color:var(--muted);text-align:center}.tabs{display:flex;gap:5px;flex-wrap:wrap}.tabs button{border:0;background:#e9ece7;border-radius:6px;padding:5px 8px;cursor:pointer}.tabs button.active{background:#18392e;color:white}.timeline{border-left:3px solid #cbd8d0;padding-left:14px;margin-left:5px}.timeline .card{position:relative}.timeline .card:before{content:"";position:absolute;left:-22px;top:17px;width:11px;height:11px;border-radius:50%;background:var(--green)}@media(max-width:1050px){.workspace{grid-template-columns:1fr;height:auto}.pane{min-height:60vh;border-right:0;border-bottom:1px solid var(--line)}.metrics{grid-template-columns:repeat(3,1fr)}}
</style>
</head>
<body>
<header class="top"><div class="topline"><div><div class="brand">Argument Workbench</div><div id="projectTitle"></div></div><div class="version"><button class="history-button" id="historyButton">Argument History</button><label for="version">稿件版本</label><select id="version"></select></div></div><div class="metrics" id="metrics"></div></header>
<main class="workspace"><section class="pane"><div class="pane-head"><h2>Manuscript · 原文</h2></div><div class="pane-body" id="manuscript"></div></section><section class="pane"><div class="pane-head"><h2>Argument · 论证</h2></div><div class="pane-body"><div id="claims" class="claim-list"></div><div id="claimDetail"></div></div></section><section class="pane"><div class="pane-head"><h2>Review · 审查</h2></div><div class="pane-body"><div id="lensTabs" class="tabs"></div><div id="review"></div></div></section></main>
<dialog id="decisionDialog"><form method="dialog" id="decisionForm"><h2 id="decisionTitle">人工裁决</h2><div id="decisionError"></div><label for="decisionValue">决定</label><select id="decisionValue"><option value="accept">接受</option><option value="reject">拒绝</option><option value="defer">推迟</option></select><label for="decisionReason">理由（必填）</label><textarea id="decisionReason" rows="3"></textarea><div id="actionFields"><label for="actionType">修改行动</label><select id="actionType"><option value="narrow_claim">收窄主张</option><option value="add_evidence">增加证据</option><option value="add_qualification">增加限定</option><option value="remove_claim">删除主张</option><option value="restructure_argument">重组论证</option><option value="clarify_concept">澄清概念</option><option value="verify_citation">核验引文</option><option value="other">其他</option></select><label for="actionText">行动说明（接受时必填）</label><textarea id="actionText" rows="3"></textarea></div><div class="dialog-actions"><button value="cancel">取消</button><button class="primary" id="saveDecision" value="default">保存正式决定</button></div></form></dialog>
<dialog id="historyDialog"><form method="dialog"><h2>Argument History</h2><p class="muted">每个数字是可审计的工作流状态，不是稿件质量分数。</p><div id="historyTimeline" class="timeline"></div><div class="dialog-actions"><button class="primary">关闭</button></div></form></dialog>
<script>
const TOKEN=__WORKBENCH_TOKEN__;let state=null,selectedClaim=null,selectedLens='all',pendingFinding=null;const $=id=>document.getElementById(id);const esc=s=>{const d=document.createElement('div');d.textContent=s??'';return d.innerHTML};
async function requestJson(path,options,mutation=false){let r;try{r=await fetch(path,options)}catch(cause){const e=Error('无法连接本地服务，请确认本次启动的页面仍然有效');e.transportFailure=true;throw e}let j;try{j=await r.json()}catch(cause){const e=Error('本地服务返回了无法识别的响应');e.transportFailure=true;throw e}if(!r.ok){const e=Error(j.error||`请求失败（${r.status}）`);e.uncertainMutation=mutation&&r.status>=500;throw e}return j}
async function load(version){const q=version?'?version='+encodeURIComponent(version):'';state=await requestJson('/api/view'+q,{headers:{'X-Argument-Workbench-Token':TOKEN}});if(!selectedClaim||!state.claims.some(c=>c.id===selectedClaim))selectedClaim=state.claims[0]?.id||null;render()}
function render(){document.title=state.project.title+' · Argument Workbench';$('projectTitle').textContent=state.project.title+' · '+state.project.source_name;$('version').innerHTML=state.project.versions.map(v=>`<option ${v===state.project.version_id?'selected':''}>${esc(v)}</option>`).join('');const d=state.dashboard;const ms=[['claims','Claims'],['open_findings','未裁决'],['deferred','推迟'],['resolved','已解决'],['accepted','已接受'],['unverified_citations','未核验引文']];$('metrics').innerHTML=ms.map(([k,l])=>`<div class="metric"><b>${d[k]}</b><span>${l}</span></div>`).join('');renderManuscript();renderClaims();renderReview()}
function renderManuscript(){$('manuscript').innerHTML=state.manuscript.map(l=>`<div class="line ${l.claim_ids.includes(selectedClaim)?'active':''}" data-claims="${l.claim_ids.join(',')}"><span class="ln">${l.number}</span><span>${esc(l.text)}${l.claim_ids.map(id=>`<button class="claim-chip" data-claim="${id}">${id}</button>`).join('')}</span></div>`).join('');document.querySelectorAll('[data-claim]').forEach(b=>b.onclick=()=>selectClaim(b.dataset.claim))}
function nodeLink(id){const n=state.nodes[id];return n?`<div class="card"><span class="claim-id">${esc(id)}</span> ${esc(n.text)}</div>`:`<div class="card">${esc(id)}</div>`}
function renderClaims(){$('claims').innerHTML=state.claims.map(c=>`<button class="${c.id===selectedClaim?'active':''}" data-select="${c.id}"><span class="claim-id">${c.id}</span> <span class="badge">${esc(c.role)}</span><div>${esc(c.text)}</div></button>`).join('');document.querySelectorAll('[data-select]').forEach(b=>b.onclick=()=>selectClaim(b.dataset.select));const c=state.claims.find(x=>x.id===selectedClaim);if(!c){$('claimDetail').innerHTML='<div class="empty">尚无 Claim</div>';return}const incoming=c.incoming.map(r=>nodeLink(r.from)+`<div class="relation">${esc(r.id)} · ${esc(r.type)} → ${esc(r.to)}</div>`).join('');const outgoing=c.outgoing.map(r=>nodeLink(r.to)+`<div class="relation">${esc(r.id)} · ${esc(r.from)} → ${esc(r.type)}</div>`).join('');$('claimDetail').innerHTML=`<div class="section"><h3>当前主张</h3><div class="card"><b>${esc(c.source_quote)}</b><p>${esc(c.text)}</p><span class="badge">${esc(c.types.join(' / '))}</span><span class="badge">${esc(c.methods.join(' / '))}</span><p class="muted">${esc(c.position)} · 位置为 deterministic；语义为 model-derived / human-corrected</p></div></div><div class="section"><h3>上游 · Supported by / Assumptions / Citations</h3>${incoming||'<div class="empty">没有上游关系</div>'}</div><div class="section"><h3>下游 · Supports / Qualifies / Contradicts</h3>${outgoing||'<div class="empty">没有下游关系</div>'}</div>`}
function provenanceTrace(f){const p=f.provenance_trace;const row=(label,value)=>value?`<div class="relation">${label} · ${esc(value)}</div>`:'';return `<details><summary>完整 provenance</summary>${row('Source',p.source_sha256)}${row('Reviewed IR',p.reviewed_ir_sha256)}${row('Review run',p.review_run_sha256)}${row('Lens protocol',p.lens_protocol_sha256)}${row('Model result',p.model_result_sha256)}${row('Finding',p.finding_sha256)}${row('Human decision',p.adjudication_sha256)}${(p.action_sha256s||[]).map((x,i)=>row('RevisionAction '+(i+1),x)).join('')}</details>`}
function lensBasis(o){const b=o.lens_basis||{},lens=state.lenses.find(l=>l.review_id===o.review_id);const rule=`<p><b>${esc(b.label)}</b></p>${b.question?`<p>检查问题：${esc(b.question)}</p>`:''}${b.failure_condition?`<p>失败条件：${esc(b.failure_condition)}</p>`:''}${b.evidence_policy?`<p class="muted">Evidence policy：${esc(b.evidence_policy)}</p>`:''}`;const protocol=lens?.protocol_text?`<pre class="protocol">${esc(lens.protocol_text)}</pre>`:'';return `<details><summary>Lens 的规则／方法论依据</summary>${rule}${protocol}</details>`}
function renderHistory(){const versions=state.version_history.map(v=>`<div class="card"><b>${esc(v.version_id)} · ${esc(v.source_name)}</b><p>${v.claims} Claims · ${v.corrections} 人工 correction · ${v.findings.open} 未裁决 · ${v.findings.accept} 接受 · ${v.findings.defer} 推迟 · ${v.unverified_citations} 未核验 Citation</p><div class="relation">Source · ${esc(v.source_sha256)}</div></div>`).join('');const transitions=state.lineage.map(h=>`<div class="card"><b>${esc(h.pair)} · Claim Lineage</b><p>${h.proposals.length} correspondences · ${Object.entries(h.summary||{}).map(([k,v])=>esc(k)+': '+v).join(' · ')}</p><div class="human">${h.proposals.filter(p=>p.human_decision).length}/${h.proposals.length} human-confirmed</div></div>`).join('');const resolutions=state.resolutions.map(r=>`<div class="card"><b>${esc(r.resolution_id)} · ${esc(r.original_finding_id)}</b><p>${esc(r.original_finding.reason)}</p><div>${esc((r.descendant_claims||[]).join(', ')||'removed')} · ${esc(r.human_decision?.final_status||r.proposed_status||'pending')}</div></div>`).join('');$('historyTimeline').innerHTML=versions+transitions+resolutions;$('historyDialog').showModal()}
function renderReview(){
  const lenses=[{id:'all',label:'全部 Lenses'},...state.lenses.map(l=>({id:l.review_id,label:l.id}))];
  $('lensTabs').innerHTML=lenses.map(l=>`<button data-lens="${esc(l.id)}" class="${l.id===selectedLens?'active':''}">${esc(l.label)}</button>`).join('');
  document.querySelectorAll('[data-lens]').forEach(b=>b.onclick=()=>{selectedLens=b.dataset.lens;renderReview()});
  const target=state.project.version_id+':'+selectedClaim;
  const outcomes=state.outcomes.filter(o=>o.target_claim===target&&(selectedLens==='all'||o.review_id===selectedLens));
  const findings=new Map(state.findings.map(f=>[f.finding_id,f]));
  const html=outcomes.map(o=>{
    const f=o.finding_id?findings.get(o.finding_id):null,decision=f?.decision||null;
    const buttons=f&&state.permissions.can_adjudicate?`<div class="decision"><button data-decide="${esc(f.finding_id)}">${decision?'复议':'人工裁决'}</button></div>`:'';
    const actions=f?.actions?.map(a=>`<li>${esc(a.action_type)} · ${esc(a.text)}</li>`).join('')||'';
    return `<div class="card verdict-${esc(o.verdict)}"><div><span class="status">${esc(o.verdict)}</span> · <b>${esc(o.lens.id)}</b> ${o.check_id?'· '+esc(o.check_id):''}</div><p>${esc(o.reason)}</p>${o.basis_refs?`<p class="muted">依据：${esc(o.basis_refs.join(', '))}</p>`:''}${o.consequence?`<p class="muted">影响：${esc(o.consequence)}</p>`:''}${lensBasis(o)}${f?`<div class="human">人工决定：${decision?esc(decision)+' · '+esc(f.human_reason):'尚未裁决'}</div>${actions?'<ul>'+actions+'</ul>':''}${provenanceTrace(f)}`:''}${buttons}</div>`
  }).join('');
  const cite=state.citations.filter(c=>(c.dependent_claims||[]).includes(selectedClaim)||state.relations.some(r=>r.from===c.id&&r.to===selectedClaim)).map(c=>`<div class="card"><b>${esc(c.id)} · ${esc(c.text)}</b><div class="${c.verification_state==='verified'?'deterministic':'model'}">${esc(c.verification_state)}</div></div>`).join('');
  const history=state.lineage.filter(x=>x.pair.includes(state.project.version_id)).flatMap(x=>x.proposals.filter(p=>(p.from_claims||[]).includes(target)||(p.to_claims||[]).includes(target))).map(p=>`<div class="card"><b>${esc(p.relation)}</b> · ${esc((p.from_claims||[]).join(', ')||'new')} → ${esc((p.to_claims||[]).join(', ')||'removed')}<div class="human">${p.human_decision?'人工：'+esc(p.human_decision.decision)+' · '+esc(p.human_decision.human_note):'等待人工确认'}</div></div>`).join('');
  const resolutions=state.resolutions.filter(r=>(r.descendant_claims||[]).includes(target)||r.original_finding.target_claim===target).map(r=>{const final=r.human_decision?.final_status||'等待人工确认';const actions=r.revision_actions.map(a=>`<li>${esc(a.action_type)} · ${esc(a.text)}</li>`).join('');return `<div class="card"><b>${esc(r.resolution_id)} · ${esc(final)}</b><p>原问题：${esc(r.original_finding.reason)}</p><p class="muted">原 Lens：${esc(r.lens.id)}${r.lens.check_id?' · '+esc(r.lens.check_id):''}</p>${actions?'<ul>'+actions+'</ul>':''}<p>${esc((r.descendant_claims||[]).join(', ')||'removed')} · 重测提案 ${esc(r.proposed_status||'pending')}</p>${r.human_decision?`<div class="human">人工确认：${esc(r.human_decision.reason)}</div>`:''}</div>`}).join('');
  $('review').innerHTML=(html||'<div class="empty">这个 Claim 在所选 Lens 下没有当前结果</div>')+`<div class="section"><h3>Citation provenance</h3>${cite||'<div class="empty">没有绑定的 Citation provenance</div>'}</div><div class="section"><h3>Claim Lineage</h3>${history||'<div class="empty">尚无跨版本 Lineage</div>'}</div><div class="section"><h3>Finding Resolution</h3>${resolutions||'<div class="empty">没有继承的旧 Finding</div>'}</div>`;
  document.querySelectorAll('[data-decide]').forEach(b=>b.onclick=()=>openDecision(b.dataset.decide));
}
function selectClaim(id){selectedClaim=id;renderManuscript();renderClaims();renderReview();document.querySelector(`.line[data-claims*="${CSS.escape(id)}"]`)?.scrollIntoView({behavior:'smooth',block:'center'})}
function openDecision(id){pendingFinding=id;const f=state.findings.find(x=>x.finding_id===id);$('decisionTitle').textContent=(f?.decision?'复议 ':'裁决 ')+id;$('decisionValue').value=f?.decision||'accept';$('decisionReason').value=f?.human_reason||'';$('actionText').value='';$('decisionError').innerHTML='';toggleAction();$('decisionDialog').showModal()}
function toggleAction(){$('actionFields').style.display=$('decisionValue').value==='accept'?'block':'none'}$('decisionValue').onchange=toggleAction;$('version').onchange=()=>{selectedClaim=null;load($('version').value).catch(showFatal)};
$('historyButton').onclick=renderHistory;
$('decisionForm').onsubmit=async e=>{if(e.submitter?.value==='cancel')return;e.preventDefault();const decision=$('decisionValue').value;const actions=decision==='accept'?[{action_type:$('actionType').value,text:$('actionText').value.trim()}]:[];const payload={finding_id:pendingFinding,decision,reason:$('decisionReason').value.trim(),actions};try{state=await requestJson('/api/adjudications',{method:'POST',headers:{'Content-Type':'application/json','X-Argument-Workbench-Token':TOKEN},body:JSON.stringify(payload)},true);$('decisionDialog').close();render()}catch(err){const uncertain=err.transportFailure||err.uncertainMutation;if(uncertain){try{await load($('version').value)}catch(ignore){}}const message=uncertain?'本地服务连接中断或发生内部错误；状态已尝试刷新。操作可能已经完成，请先检查当前裁决，不要直接重复提交。':err.message;$('decisionError').innerHTML=`<div class="error">${esc(message)}</div>`}}
function showFatal(err){document.body.innerHTML=`<div class="error" style="margin:30px">${esc(err.message)}</div>`}load().catch(showFatal);
</script>
</body></html>'''


__all__ = [
    "LocalWorkbench",
    "WorkbenchHTTPServer",
    "adjudicate_from_ui",
    "build_project_view",
    "render_app_shell",
    "serve_workbench",
]
