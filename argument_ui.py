"""Local, document-first application layer for Argument Workbench.

The core engine does not import this module.  The UI reads validated artifacts
and delegates every mutation to an existing domain service, so opening the UI
cannot turn model output into human-confirmed state.
"""

from __future__ import annotations

import json
import hmac
import re
import secrets
import socket
import threading
import time
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


class LocalHTTPProtocolError(Exception):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


def request_context(token: str, project: Path, view: dict[str, Any]) -> str:
    """Bind a displayed, deterministic snapshot to its actual project and session."""
    body = json.dumps([str(project.resolve()), view], sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hmac.new(token.encode("ascii"), body.encode("ascii"), "sha256").hexdigest()


def require_request_context(handler, expected: str) -> None:
    values = handler.headers.get_all("X-Argument-Project-Context", [])
    if len(values) != 1 or not values[0].isascii() or not secrets.compare_digest(values[0], expected):
        raise LocalHTTPProtocolError(HTTPStatus.CONFLICT, "项目或任务已变化，请刷新页面并核对内容后再提交")


class LocalHTTPServer(ThreadingHTTPServer):
    """Bound resource use for all local UI entry points."""
    daemon_threads = True
    request_queue_size = 16
    max_connections = 8

    def __init__(self, address, handler):
        self.connection_slots = threading.BoundedSemaphore(self.max_connections)
        super().__init__(address, handler)

    def process_request(self, request, client_address):
        if not self.connection_slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nContent-Length: 16\r\nConnection: close\r\n\r\nToo many clients")
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.connection_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connection_slots.release()


class LocalRequestHandler(BaseHTTPRequestHandler):
    timeout = 30
    header_timeout = 10
    body_timeout = 30

    def setup(self):
        super().setup()
        # Socket inactivity alone permits a client to retain a slot forever by
        # sending one byte per timeout. Headers have an absolute deadline too.
        self._header_timer = threading.Timer(self.header_timeout, self._expire_headers)
        self._header_timer.daemon = True
        self._header_timer.start()

    def _expire_headers(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def parse_request(self):
        try:
            return super().parse_request()
        finally:
            self._header_timer.cancel()

    def handle(self):
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def finish(self):
        self._header_timer.cancel()
        try:
            super().finish()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _local_request(self) -> bool:
        try:
            hosts = self.headers.get_all("Host", [])
            origins = self.headers.get_all("Origin", [])
            if len(hosts) != 1 or len(origins) > 1 or any(c.isspace() for c in hosts[0]):
                return False
            host = urlsplit("http://" + hosts[0])
            target = urlsplit(self.path)
            if (host.hostname not in LOOPBACK_HOSTS or host.port != self.server.server_address[1]
                    or host.username is not None or host.password is not None
                    or host.path or host.query or host.fragment
                    or not self.path.startswith("/") or target.scheme or target.netloc):
                return False
            return not origins or origins[0] == f"http://{host.netloc}"
        except ValueError:
            return False

    def _token_authorized(self, name: str) -> bool:
        values = self.headers.get_all(name, [])
        return len(values) == 1 and values[0].isascii() and secrets.compare_digest(values[0], self.server.app.token)

    def _read_json_body(self, maximum: int) -> bytes:
        if (len(self.headers.get_all("Content-Type", [])) != 1
                or self.headers.get_content_type() != "application/json"
                or (self.headers.get_content_charset() or "utf-8").lower() not in {"utf-8", "utf8"}):
            raise LocalHTTPProtocolError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "JSON UTF-8 required")
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding", []) or len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
            raise LocalHTTPProtocolError(HTTPStatus.BAD_REQUEST, "请求长度头无效")
        # Avoid conversion of an attacker-controlled, arbitrarily long integer.
        if len(lengths[0]) > 12:
            raise LocalHTTPProtocolError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请求大小无效")
        length = int(lengths[0])
        if length <= 0 or length > maximum:
            raise LocalHTTPProtocolError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请求大小无效")
        deadline = time.monotonic() + self.body_timeout
        chunks = []
        remaining = length
        try:
            while remaining:
                wait = deadline - time.monotonic()
                if wait <= 0:
                    raise TimeoutError
                self.connection.settimeout(wait)
                chunk = self.rfile.read1(min(remaining, 65536))
                if not chunk:
                    raise LocalHTTPProtocolError(HTTPStatus.BAD_REQUEST, "请求正文不完整")
                chunks.append(chunk)
                remaining -= len(chunk)
        except TimeoutError as exc:
            raise LocalHTTPProtocolError(HTTPStatus.REQUEST_TIMEOUT, "请求正文读取超时，请重新提交") from exc
        finally:
            self.connection.settimeout(self.timeout)
        return b"".join(chunks)


POSITION_PATTERN = re.compile(
    r"L(?P<start_line>[1-9][0-9]*):C(?P<start_column>[1-9][0-9]*)"
    r"-L(?P<end_line>[1-9][0-9]*):C(?P<end_column>[1-9][0-9]*)\Z"
)


def _source(workspace) -> tuple[str, dict[str, Any]]:
    from argument_workbench import _decoded_workspace_text, _workspace_source

    version, source_bytes, _ = _workspace_source(workspace)
    return _decoded_workspace_text(version, source_bytes), version


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
        value = build_project_view(self.project_dir, version_id)
        return {**value, "request_context": request_context(self.token, self.project_dir, value)}

    def adjudicate(self, payload: dict[str, Any]) -> dict[str, Any]:
        adjudicate_from_ui(self.project_dir, payload)
        return self.view()


class WorkbenchHTTPServer(LocalHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: LocalWorkbench):
        self.app = app
        self.action_lock = threading.RLock()
        super().__init__(address, WorkbenchRequestHandler)


class WorkbenchRequestHandler(LocalRequestHandler):
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
        return self._token_authorized("X-Argument-Workbench-Token")

    def do_GET(self) -> None:  # noqa: N802
        if not self._local_request():
            self._json(HTTPStatus.FORBIDDEN, {"error": "只接受当前本机地址和同源页面"})
            return
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
        if not self._local_request():
            self._json(HTTPStatus.FORBIDDEN, {"error": "只接受当前本机地址和同源页面"})
            return
        if urlsplit(self.path).path != "/api/adjudications":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if not self._authorized():
            self._json(HTTPStatus.FORBIDDEN, {"error": "local UI token required"})
            return
        try:
            payload = parse_json_strict(self._read_json_body(MAX_REQUEST_BYTES))
            if not isinstance(payload, dict):
                raise WorkbenchError("request body must be an object")
            with self.server.action_lock, project_mutation_lock(self.server.app.project_dir):
                require_request_context(self, self.server.app.view()["request_context"])
                result = self.server.app.adjudicate(payload)
        except LocalHTTPProtocolError as exc:
            self._json(exc.status, {"error": str(exc)})
            return
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
    token_json = json.dumps(token).replace("<", "\\u003c")
    return APP_SHELL.replace("__WORKBENCH_TOKEN__", token_json)


from studio_web.research import research_shell as _research_shell

APP_SHELL = _research_shell("professional")


__all__ = [
    "LocalWorkbench",
    "WorkbenchHTTPServer",
    "adjudicate_from_ui",
    "build_project_view",
    "render_app_shell",
    "serve_workbench",
]
