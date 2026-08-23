"""Machine-readable contracts for argument structure and method-conditional review."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any


ARGUMENT_IR_SCHEMA_VERSION = 1
IR_EXTRACTION_PROTOCOL_VERSION = 2
SUPPORTED_IR_EXTRACTION_PROTOCOL_VERSIONS = (1, 2)
CHECK_LIBRARY_SCHEMA_VERSION = 3
SUPPORTED_CHECK_LIBRARY_SCHEMA_VERSIONS = (1, 2, 3)
CHECK_PLAN_SCHEMA_VERSION = 3
SUPPORTED_CHECK_PLAN_SCHEMA_VERSIONS = (1, 2, 3)
CHECK_RESULTS_SCHEMA_VERSION = 3
SUPPORTED_CHECK_RESULTS_SCHEMA_VERSIONS = (1, 2, 3)
ARGUMENT_FINDINGS_SCHEMA_VERSION = 1

CLAIM_TYPES = (
    "normative",
    "conceptual",
    "interpretive",
    "descriptive",
    "causal",
    "predictive",
    "evaluative",
)
METHOD_TYPES = (
    "conceptual-analysis",
    "interpretive-analysis",
    "descriptive-empirical",
    "causal-observational",
    "causal-experimental",
    "quantitative",
    "qualitative",
    "comparative-historical",
    "predictive-modeling",
    "normative-reasoning",
    "evaluative-analysis",
    "unspecified",
    "other",
)
CLAIM_ROLES = ("premise", "intermediate", "conclusion", "background")
EXTRACTION_MODES = ("explicit", "inferred")
EVIDENCE_KINDS = (
    "data",
    "observation",
    "case",
    "quotation",
    "formal-result",
    "authority",
    "document",
    "other",
)
RELATION_TYPES = ("supports", "contradicts", "qualifies", "assumes", "cites")
CHECK_TIERS = ("core", "extended")
CHECK_DEPTHS = ("core", "full")
REVIEW_SCOPES = ("thesis-chain", "claim", "claims", "all")
EVIDENCE_POLICIES = (
    "claim-text-sufficient",
    "upstream-required",
    "citation-required",
)
RESULT_VERDICTS = ("pass", "fail", "uncertain")
RESULT_EXECUTION_STATUSES = (
    "evaluated",
    "blocked_missing_context",
    "routing_mismatch",
    "not_applicable",
)
RESULT_STATUSES = ("complete", "partial")
REQUIRED_CONTEXT_VALUES = (
    "claim",
    "source_quote",
    "methods",
    "incoming_support",
    "assumptions",
    "citations",
)

IR_KEYS = {
    "schema_version",
    "artifact",
    "scope",
    "source",
    "claims",
    "evidence",
    "assumptions",
    "citations",
    "relations",
    "unverified",
}
CLAIM_KEYS = {
    "id",
    "text",
    "source_quote",
    "position",
    "types",
    "methods",
    "role",
    "extraction",
    "uncertainty",
}
EVIDENCE_KEYS = {"id", "text", "source_quote", "position", "kind"}
ASSUMPTION_KEYS = {
    "id",
    "text",
    "source_quote",
    "position",
    "extraction",
    "uncertainty",
}
CITATION_KEYS = {"id", "text", "source_quote", "position", "locator"}
RELATION_KEYS = {"id", "type", "from", "to"}
CHECK_KEYS_V1 = {
    "id",
    "label",
    "category",
    "tier",
    "applies_to",
    "question",
    "failure_condition",
    "required_context",
}
CHECK_KEYS = CHECK_KEYS_V1 | {"evidence_policy"}
TASK_KEYS = {"id", "claim_id", "check_id"}
RESULT_KEYS_V1 = {"task_id", "verdict", "reason", "evidence_refs", "consequence"}
RESULT_KEYS = {
    "task_id",
    "execution_status",
    "verdict",
    "reason",
    "basis_refs",
    "support_refs",
    "consequence",
}
RESULT_KEYS_V3 = RESULT_KEYS | {"support_paths"}
SUPPORT_PATH_KEYS = {"support_ref", "relation_ids"}
PASS_SUPPORT_RELATION_TYPES = {"supports", "qualifies", "cites"}
FINDING_EVIDENCE_KEYS = {
    "node_id",
    "node_kind",
    "text",
    "source_quote",
    "position",
}
FINDING_KEYS = {
    "id",
    "task_id",
    "claim_id",
    "check_id",
    "verdict",
    "claim_text",
    "position",
    "reason",
    "consequence",
    "evidence_refs",
    "evidence",
}

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_CHECK_ID_PATTERN = re.compile(r"[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+\Z")


class ArgumentIRError(ValueError):
    """Raised when an IR artifact cannot be derived safely."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_sha256(value: object) -> str:
    return _sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _safe_basename(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and "/" not in value
        and "\\" not in value
        and value not in {".", ".."}
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _occurrence_offsets(text: str, fragment: str) -> list[int]:
    if not fragment:
        return []
    offsets: list[int] = []
    cursor = 0
    while True:
        offset = text.find(fragment, cursor)
        if offset < 0:
            return offsets
        offsets.append(offset)
        cursor = offset + 1


def _validate_digest(value: object, label: str, errors: list[str]) -> None:
    if not _is_digest(value):
        errors.append(f"{label} must be a lowercase SHA-256 digest")


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and _SHA256_PATTERN.fullmatch(value) is not None


def _validate_string_list(
    value: object,
    label: str,
    errors: list[str],
    *,
    allowed: tuple[str, ...] | None = None,
    allow_empty: bool,
) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"{label} must be an array")
        return []
    if not allow_empty and not value:
        errors.append(f"{label} must not be empty")
    if any(not _nonempty_string(item) for item in value):
        errors.append(f"{label} must contain non-empty strings")
        return []
    strings = [str(item) for item in value]
    if len(strings) != len(set(strings)):
        errors.append(f"{label} must not contain duplicates")
    if allowed is not None:
        unknown = sorted(set(strings) - set(allowed))
        if unknown:
            errors.append(f"{label} contains unknown values: {unknown}")
    return strings


def _validate_continuous_ids(
    items: object,
    label: str,
    prefix: str,
    errors: list[str],
) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        errors.append(f"{label} must be an array")
        return []
    typed = [item for item in items if isinstance(item, dict)]
    if len(typed) != len(items):
        errors.append(f"{label} must contain objects")
        return typed
    actual = [item.get("id") for item in typed]
    expected = [f"{prefix}{index}" for index in range(1, len(typed) + 1)]
    if actual != expected:
        errors.append(f"{label} IDs must be continuous and ordered: expected {expected}, got {actual}")
    return typed


def _validate_provenance_fields(
    item: dict[str, Any],
    label: str,
    errors: list[str],
    manuscript_text: str | None,
) -> None:
    for key in ("text", "source_quote", "position"):
        if not _nonempty_string(item.get(key)):
            errors.append(f"{label}.{key} must be a non-empty string")
    quote = item.get("source_quote")
    if manuscript_text is not None and isinstance(quote, str):
        occurrence_offsets = _occurrence_offsets(manuscript_text, quote)
        if not occurrence_offsets:
            errors.append(
                f"{label}.source_quote is not an exact substring of the source manuscript"
            )
        elif len(occurrence_offsets) > 1:
            errors.append(
                f"{label}.source_quote is ambiguous ({len(occurrence_offsets)} exact occurrences); "
                "use a longer unique quote"
            )


def _directed_cycle(adjacency: dict[str, list[str]]) -> list[str] | None:
    indegree = {node: 0 for node in adjacency}
    for targets in adjacency.values():
        for target in targets:
            if target in indegree:
                indegree[target] += 1
    ready = [node for node, degree in indegree.items() if degree == 0]
    visited = 0
    cursor = 0
    while cursor < len(ready):
        node = ready[cursor]
        cursor += 1
        visited += 1
        for target in adjacency.get(node, []):
            if target not in indegree:
                continue
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if visited == len(indegree):
        return None
    return [node for node, degree in indegree.items() if degree > 0]


def validate_argument_ir(
    value: object,
    *,
    source_bytes: bytes | None = None,
    source_name: str | None = None,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["argument IR must be a JSON object"]
    if set(value) != IR_KEYS:
        errors.append("argument IR must contain exactly the v1 top-level fields")
    if value.get("schema_version") != ARGUMENT_IR_SCHEMA_VERSION:
        errors.append("schema_version must be 1")
    if value.get("artifact") != "argument-ir":
        errors.append("artifact must be argument-ir")
    if value.get("scope") != "social-science":
        errors.append("scope must be social-science in IR v1")

    manuscript_text: str | None = None
    if source_bytes is not None:
        try:
            manuscript_text = source_bytes.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            errors.append(f"source manuscript is not UTF-8: {exc}")

    source = value.get("source")
    if not isinstance(source, dict) or set(source) != {"name", "sha256"}:
        errors.append("source must contain exactly name and sha256")
    else:
        if not _safe_basename(source.get("name")):
            errors.append("source.name must be a safe basename")
        _validate_digest(source.get("sha256"), "source.sha256", errors)
        if source_name is not None and source.get("name") != source_name:
            errors.append("source.name does not match the supplied manuscript")
        if source_bytes is not None and source.get("sha256") != _sha256(source_bytes):
            errors.append("source.sha256 does not match the supplied manuscript bytes")

    claims = _validate_continuous_ids(value.get("claims"), "claims", "C", errors)
    if not claims:
        errors.append("claims must contain at least one argumentative claim")
    evidence = _validate_continuous_ids(value.get("evidence"), "evidence", "E", errors)
    assumptions = _validate_continuous_ids(
        value.get("assumptions"), "assumptions", "A", errors
    )
    citations = _validate_continuous_ids(
        value.get("citations"), "citations", "Z", errors
    )
    relations = _validate_continuous_ids(
        value.get("relations"), "relations", "R", errors
    )

    for index, claim in enumerate(claims):
        label = f"claims[{index}]"
        if set(claim) != CLAIM_KEYS:
            errors.append(f"{label} must contain exactly the claim fields")
        _validate_provenance_fields(claim, label, errors, manuscript_text)
        _validate_string_list(
            claim.get("types"), label + ".types", errors, allowed=CLAIM_TYPES, allow_empty=False
        )
        methods = _validate_string_list(
            claim.get("methods"),
            label + ".methods",
            errors,
            allowed=METHOD_TYPES,
            allow_empty=False,
        )
        if len(methods) > 1 and ({"unspecified", "other"} & set(methods)):
            errors.append(
                f"{label}.methods must use unspecified or other alone"
            )
        if claim.get("role") not in CLAIM_ROLES:
            errors.append(f"{label}.role must be one of {CLAIM_ROLES}")
        extraction = claim.get("extraction")
        if extraction not in EXTRACTION_MODES:
            errors.append(f"{label}.extraction must be explicit or inferred")
        if not isinstance(claim.get("uncertainty"), str):
            errors.append(f"{label}.uncertainty must be a string")
        elif extraction == "inferred" and not claim["uncertainty"].strip():
            errors.append(f"{label}.uncertainty is required for inferred claims")

    for index, item in enumerate(evidence):
        label = f"evidence[{index}]"
        if set(item) != EVIDENCE_KEYS:
            errors.append(f"{label} must contain exactly the evidence fields")
        _validate_provenance_fields(item, label, errors, manuscript_text)
        if item.get("kind") not in EVIDENCE_KINDS:
            errors.append(f"{label}.kind must be one of {EVIDENCE_KINDS}")

    for index, item in enumerate(assumptions):
        label = f"assumptions[{index}]"
        if set(item) != ASSUMPTION_KEYS:
            errors.append(f"{label} must contain exactly the assumption fields")
        _validate_provenance_fields(item, label, errors, manuscript_text)
        extraction = item.get("extraction")
        if extraction not in EXTRACTION_MODES:
            errors.append(f"{label}.extraction must be explicit or inferred")
        if not isinstance(item.get("uncertainty"), str):
            errors.append(f"{label}.uncertainty must be a string")
        elif extraction == "inferred" and not item["uncertainty"].strip():
            errors.append(f"{label}.uncertainty is required for inferred assumptions")

    for index, item in enumerate(citations):
        label = f"citations[{index}]"
        if set(item) != CITATION_KEYS:
            errors.append(f"{label} must contain exactly the citation fields")
        _validate_provenance_fields(item, label, errors, manuscript_text)
        if not isinstance(item.get("locator"), str):
            errors.append(f"{label}.locator must be a string")

    nodes: dict[str, tuple[str, dict[str, Any]]] = {}
    for kind, items in (
        ("claim", claims),
        ("evidence", evidence),
        ("assumption", assumptions),
        ("citation", citations),
    ):
        for item in items:
            identifier = item.get("id")
            if isinstance(identifier, str):
                if identifier in nodes:
                    errors.append(f"duplicate node ID: {identifier}")
                nodes[identifier] = (kind, item)

    allowed_endpoints = {
        "supports": ({"claim", "evidence"}, {"claim"}),
        "contradicts": ({"claim", "evidence"}, {"claim"}),
        "qualifies": ({"claim", "evidence"}, {"claim"}),
        "assumes": ({"assumption"}, {"claim"}),
        "cites": ({"citation"}, {"claim", "evidence"}),
    }
    seen_relations: set[tuple[object, object, object]] = set()
    for index, relation in enumerate(relations):
        label = f"relations[{index}]"
        if set(relation) != RELATION_KEYS:
            errors.append(f"{label} must contain exactly the relation fields")
        relation_type = relation.get("type")
        if relation_type not in RELATION_TYPES:
            errors.append(f"{label}.type must be one of {RELATION_TYPES}")
            continue
        source_id = relation.get("from")
        target_id = relation.get("to")
        if source_id == target_id:
            errors.append(f"{label} must not be a self-relation")
        source_node = nodes.get(str(source_id))
        target_node = nodes.get(str(target_id))
        if source_node is None:
            errors.append(f"{label}.from references an unknown node: {source_id!r}")
        if target_node is None:
            errors.append(f"{label}.to references an unknown node: {target_id!r}")
        if source_node is not None and target_node is not None:
            allowed_from, allowed_to = allowed_endpoints[str(relation_type)]
            if source_node[0] not in allowed_from or target_node[0] not in allowed_to:
                errors.append(
                    f"{label} has invalid endpoint kinds for {relation_type}: "
                    f"{source_node[0]} -> {target_node[0]}"
                )
        triple = (relation_type, source_id, target_id)
        if triple in seen_relations:
            errors.append(f"{label} duplicates an existing relation")
        seen_relations.add(triple)

    support_adjacency: dict[str, list[str]] = {
        claim.get("id"): []
        for claim in claims
        if isinstance(claim.get("id"), str)
    }
    for relation in relations:
        if relation.get("type") not in {"supports", "qualifies"}:
            continue
        source_id = relation.get("from")
        target_id = relation.get("to")
        if source_id in support_adjacency and target_id in support_adjacency:
            support_adjacency[source_id].append(target_id)
    cycle = _directed_cycle(support_adjacency)
    if cycle is not None:
        errors.append(
            "claim support/qualification graph must be acyclic; cycle prevents ordering of: "
            + ", ".join(cycle)
        )

    _validate_string_list(
        value.get("unverified"), "unverified", errors, allow_empty=True
    )
    return errors


def _canonical_position(manuscript: str, quote: str) -> str:
    start = manuscript.index(quote)
    end = start + len(quote)

    def line_column(offset: int) -> tuple[int, int]:
        line = manuscript.count("\n", 0, offset) + 1
        previous_newline = manuscript.rfind("\n", 0, offset)
        column = offset - previous_newline
        return line, column

    start_line, start_column = line_column(start)
    end_line, end_column = line_column(end)
    return f"L{start_line}:C{start_column}-L{end_line}:C{end_column}"


def canonicalize_argument_ir(
    value: object,
    *,
    source_bytes: bytes,
    source_name: str,
) -> dict[str, Any]:
    errors = validate_argument_ir(
        value,
        source_bytes=source_bytes,
        source_name=source_name,
    )
    if errors:
        raise ArgumentIRError("; ".join(errors))
    assert isinstance(value, dict)
    manuscript = source_bytes.decode("utf-8-sig")
    normalized = copy.deepcopy(value)
    for field in ("claims", "evidence", "assumptions", "citations"):
        for item in normalized[field]:
            item["position"] = _canonical_position(manuscript, item["source_quote"])
    return normalized


def validate_check_library(value: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["check library must be a JSON object"]
    if set(value) != {
        "schema_version",
        "artifact",
        "scope",
        "claim_types",
        "method_types",
        "checks",
    }:
        errors.append("check library must contain exactly the v1 fields")
    schema_version = value.get("schema_version")
    if schema_version not in SUPPORTED_CHECK_LIBRARY_SCHEMA_VERSIONS:
        errors.append("check library schema_version must be 1, 2, or 3")
    if value.get("artifact") != "argument-check-library":
        errors.append("check library artifact must be argument-check-library")
    if value.get("scope") != "social-science":
        errors.append("check library scope must be social-science")
    if value.get("claim_types") != list(CLAIM_TYPES):
        errors.append("check library claim_types must equal the IR v1 registry")
    if value.get("method_types") != list(METHOD_TYPES):
        errors.append("check library method_types must equal the IR v1 registry")
    checks = value.get("checks")
    if not isinstance(checks, list) or not checks:
        errors.append("checks must be a non-empty array")
        return errors
    seen_ids: set[str] = set()
    for index, check in enumerate(checks):
        label = f"checks[{index}]"
        if not isinstance(check, dict):
            errors.append(f"{label} must be an object")
            continue
        expected_check_keys = CHECK_KEYS if schema_version in {2, 3} else CHECK_KEYS_V1
        if set(check) != expected_check_keys:
            errors.append(
                f"{label} must contain exactly the v{schema_version} check fields"
            )
        check_id = check.get("id")
        if not isinstance(check_id, str) or _CHECK_ID_PATTERN.fullmatch(check_id) is None:
            errors.append(f"{label}.id must be a dotted lowercase identifier")
        elif check_id in seen_ids:
            errors.append(f"duplicate check id: {check_id}")
        else:
            seen_ids.add(check_id)
        for key in ("label", "category", "question", "failure_condition"):
            if not _nonempty_string(check.get(key)):
                errors.append(f"{label}.{key} must be a non-empty string")
        if check.get("tier") not in CHECK_TIERS:
            errors.append(f"{label}.tier must be core or extended")
        applicability = check.get("applies_to")
        if not isinstance(applicability, dict) or set(applicability) != {
            "claim_types",
            "methods",
        }:
            errors.append(f"{label}.applies_to must contain claim_types and methods")
        else:
            _validate_applicability_dimension(
                applicability.get("claim_types"),
                label + ".applies_to.claim_types",
                CLAIM_TYPES,
                errors,
            )
            _validate_applicability_dimension(
                applicability.get("methods"),
                label + ".applies_to.methods",
                METHOD_TYPES,
                errors,
            )
        _validate_string_list(
            check.get("required_context"),
            label + ".required_context",
            errors,
            allowed=REQUIRED_CONTEXT_VALUES,
            allow_empty=False,
        )
        if schema_version in {2, 3} and check.get("evidence_policy") not in EVIDENCE_POLICIES:
            errors.append(
                f"{label}.evidence_policy must be one of {EVIDENCE_POLICIES}"
            )
    return errors


def _validate_applicability_dimension(
    value: object,
    label: str,
    allowed: tuple[str, ...],
    errors: list[str],
) -> None:
    strings = _validate_string_list(value, label, errors, allow_empty=False)
    if "*" in strings and len(strings) != 1:
        errors.append(f"{label} must use '*' alone")
    unknown = sorted(set(strings) - set(allowed) - {"*"})
    if unknown:
        errors.append(f"{label} contains unknown values: {unknown}")


def _applies(required: list[object], actual: list[object]) -> bool:
    return required == ["*"] or any(
        isinstance(item, str) and item in actual for item in required
    )


def _node_registry(ir: dict[str, Any]) -> dict[str, tuple[str, dict[str, Any]]]:
    nodes: dict[str, tuple[str, dict[str, Any]]] = {}
    for kind, field in (
        ("claim", "claims"),
        ("evidence", "evidence"),
        ("assumption", "assumptions"),
        ("citation", "citations"),
    ):
        for item in ir[field]:
            nodes[item["id"]] = (kind, item)
    return nodes


def select_review_claim_ids(
    ir: dict[str, Any], review_scope: str, claim_ids: list[str]
) -> list[str]:
    known_ids = [str(claim["id"]) for claim in ir["claims"]]
    known = set(known_ids)
    if len(claim_ids) != len(set(claim_ids)):
        raise ArgumentIRError("review scope claim IDs must not repeat")
    unknown = [claim_id for claim_id in claim_ids if claim_id not in known]
    if unknown:
        raise ArgumentIRError(f"review scope contains unknown Claim IDs: {unknown}")
    if review_scope == "all":
        if claim_ids:
            raise ArgumentIRError("all review scope does not accept explicit Claim IDs")
        return known_ids
    if review_scope == "claim":
        if len(claim_ids) != 1:
            raise ArgumentIRError("claim review scope requires exactly one Claim ID")
        return claim_ids
    if review_scope == "claims":
        if not claim_ids:
            raise ArgumentIRError("claims review scope requires at least one Claim ID")
        selected = set(claim_ids)
        return [claim_id for claim_id in known_ids if claim_id in selected]

    seeds = {
        str(claim["id"])
        for claim in ir["claims"]
        if claim["role"] in {"conclusion", "intermediate"}
    }
    seeds.update(claim_ids)
    if not seeds:
        raise ArgumentIRError(
            "thesis-chain review scope requires a conclusion/intermediate Claim "
            "or an explicitly pinned --claim"
        )
    reachable = set(seeds)
    changed = True
    while changed:
        changed = False
        for relation in ir["relations"]:
            if (
                relation["type"] in {"supports", "qualifies", "assumes", "cites"}
                and relation["to"] in reachable
                and relation["from"] not in reachable
            ):
                reachable.add(relation["from"])
                changed = True
    return [claim_id for claim_id in known_ids if claim_id in reachable]


def build_check_plan(
    ir: object,
    library: object,
    *,
    ir_sha256: str,
    library_sha256: str,
    depth: str,
    review_scope: str = "all",
    claim_ids: list[str] | None = None,
) -> dict[str, Any]:
    errors = validate_argument_ir(ir)
    errors.extend(validate_check_library(library))
    if depth not in CHECK_DEPTHS:
        errors.append("depth must be core or full")
    if review_scope not in REVIEW_SCOPES:
        errors.append(f"review_scope must be one of {REVIEW_SCOPES}")
    if not _is_digest(ir_sha256):
        errors.append("ir_sha256 must be a lowercase SHA-256 digest")
    if not _is_digest(library_sha256):
        errors.append("library_sha256 must be a lowercase SHA-256 digest")
    if errors:
        raise ArgumentIRError("; ".join(errors))
    assert isinstance(ir, dict)
    assert isinstance(library, dict)
    library_version = int(library["schema_version"])
    requested_claim_ids = list(claim_ids or [])
    if library_version == 1 and (
        review_scope != "all" or requested_claim_ids
    ):
        raise ArgumentIRError("legacy v1 check libraries support only all-Claim scope")
    selected_claim_ids = select_review_claim_ids(
        ir, review_scope, requested_claim_ids
    )
    selected_claim_id_set = set(selected_claim_ids)
    tasks: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    for claim in ir["claims"]:
        if claim["id"] not in selected_claim_id_set:
            continue
        for check in library["checks"]:
            if depth == "core" and check["tier"] != "core":
                continue
            applicability = check["applies_to"]
            if not _applies(applicability["claim_types"], claim["types"]):
                continue
            if not _applies(applicability["methods"], claim["methods"]):
                continue
            selected_ids.add(check["id"])
            tasks.append(
                {
                    "id": f"T{len(tasks) + 1}",
                    "claim_id": claim["id"],
                    "check_id": check["id"],
                }
            )
    plan = {
        "schema_version": library_version,
        "artifact": "argument-check-plan",
        "depth": depth,
        "source": {
            "ir_sha256": ir_sha256,
            "argument_sha256": _canonical_sha256(ir),
            "library_sha256": library_sha256,
            "scope": ir["scope"],
        },
        "argument_ir": copy.deepcopy(ir),
        "checks": [
            copy.deepcopy(check)
            for check in library["checks"]
            if check["id"] in selected_ids
        ],
        "tasks": tasks,
    }
    if library_version in {2, 3}:
        plan["review_scope"] = {
            "kind": review_scope,
            "claim_ids": requested_claim_ids,
            "selected_claim_ids": selected_claim_ids,
        }
    return plan


def validate_check_plan(value: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["check plan must be a JSON object"]
    schema_version = value.get("schema_version")
    expected_keys = {
        "schema_version",
        "artifact",
        "depth",
        "source",
        "argument_ir",
        "checks",
        "tasks",
    }
    if schema_version in {2, 3}:
        expected_keys.add("review_scope")
    if set(value) != expected_keys:
        errors.append(f"check plan must contain exactly the v{schema_version} fields")
    if schema_version not in SUPPORTED_CHECK_PLAN_SCHEMA_VERSIONS:
        errors.append("check plan schema_version must be 1, 2, or 3")
    if value.get("artifact") != "argument-check-plan":
        errors.append("check plan artifact must be argument-check-plan")
    if value.get("depth") not in CHECK_DEPTHS:
        errors.append("check plan depth must be core or full")
    source = value.get("source")
    if not isinstance(source, dict) or set(source) != {
        "ir_sha256",
        "argument_sha256",
        "library_sha256",
        "scope",
    }:
        errors.append(
            "check plan source must contain raw IR hash, semantic argument hash, "
            "library hash, and scope"
        )
    else:
        _validate_digest(source.get("ir_sha256"), "source.ir_sha256", errors)
        _validate_digest(
            source.get("argument_sha256"), "source.argument_sha256", errors
        )
        _validate_digest(source.get("library_sha256"), "source.library_sha256", errors)
        if source.get("scope") != "social-science":
            errors.append("check plan source.scope must be social-science")

    argument = value.get("argument_ir")
    argument_errors = validate_argument_ir(argument)
    errors.extend(f"argument_ir: {error}" for error in argument_errors)
    claims = (
        argument.get("claims")
        if isinstance(argument, dict) and isinstance(argument.get("claims"), list)
        else []
    )
    claim_by_id = {
        claim["id"]: claim
        for claim in claims
        if isinstance(claim, dict) and isinstance(claim.get("id"), str)
    }
    if (
        isinstance(source, dict)
        and isinstance(argument, dict)
        and source.get("scope") != argument.get("scope")
    ):
        errors.append("source.scope must match argument_ir.scope")
    if (
        isinstance(source, dict)
        and isinstance(argument, dict)
        and not argument_errors
        and source.get("argument_sha256") != _canonical_sha256(argument)
    ):
        errors.append("source.argument_sha256 does not match argument_ir semantics")

    selected_checks = value.get("checks")
    selected_library = {
        "schema_version": schema_version,
        "artifact": "argument-check-library",
        "scope": "social-science",
        "claim_types": list(CLAIM_TYPES),
        "method_types": list(METHOD_TYPES),
        "checks": selected_checks,
    }
    check_errors = validate_check_library(selected_library)
    errors.extend(f"checks: {error}" for error in check_errors)
    checks = selected_checks if isinstance(selected_checks, list) else []
    check_by_id = {
        check["id"]: check
        for check in checks
        if isinstance(check, dict) and isinstance(check.get("id"), str)
    }

    selected_claim_ids = [
        str(claim.get("id")) for claim in claims if isinstance(claim, dict)
    ]
    if schema_version in {2, 3}:
        scope = value.get("review_scope")
        if not isinstance(scope, dict) or set(scope) != {
            "kind",
            "claim_ids",
            "selected_claim_ids",
        }:
            errors.append(
                "review_scope must contain kind, claim_ids, and selected_claim_ids"
            )
            selected_claim_ids = []
        else:
            kind = scope.get("kind")
            if kind not in REVIEW_SCOPES:
                errors.append(f"review_scope.kind must be one of {REVIEW_SCOPES}")
            requested = _validate_string_list(
                scope.get("claim_ids"),
                "review_scope.claim_ids",
                errors,
                allow_empty=True,
            )
            stored_selected = _validate_string_list(
                scope.get("selected_claim_ids"),
                "review_scope.selected_claim_ids",
                errors,
                allow_empty=False,
            )
            if (
                isinstance(argument, dict)
                and not argument_errors
                and kind in REVIEW_SCOPES
            ):
                try:
                    expected_selected = select_review_claim_ids(
                        argument, str(kind), requested
                    )
                except ArgumentIRError as exc:
                    errors.append(f"review_scope: {exc}")
                    expected_selected = []
                if stored_selected != expected_selected:
                    errors.append(
                        "review_scope.selected_claim_ids is not deterministically derived"
                    )
            selected_claim_ids = stored_selected
    selected_claim_id_set = set(selected_claim_ids)

    tasks = _validate_continuous_ids(value.get("tasks"), "tasks", "T", errors)
    seen_pairs: set[tuple[object, object]] = set()
    actual_pairs: list[tuple[object, object]] = []
    for index, task in enumerate(tasks):
        label = f"tasks[{index}]"
        if set(task) != TASK_KEYS:
            errors.append(f"{label} must contain exactly the task fields")
        claim_id = task.get("claim_id")
        check_id = task.get("check_id")
        if not isinstance(claim_id, str) or not re.fullmatch(
            r"C[1-9][0-9]*", str(claim_id)
        ):
            errors.append(f"{label}.claim_id must be C1..Cn")
        elif claim_id not in claim_by_id:
            errors.append(f"{label}.claim_id is not in argument_ir")
        if not isinstance(check_id, str) or _CHECK_ID_PATTERN.fullmatch(
            str(check_id)
        ) is None:
            errors.append(f"{label}.check_id is invalid")
        elif check_id not in check_by_id:
            errors.append(f"{label}.check_id is not in checks")
        pair = (task.get("claim_id"), task.get("check_id"))
        if pair in seen_pairs:
            errors.append(f"{label} duplicates a claim/check pair")
        seen_pairs.add(pair)
        actual_pairs.append(pair)

    depth = value.get("depth")
    expected_pairs: list[tuple[str, str]] = []
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        if claim.get("id") not in selected_claim_id_set:
            continue
        claim_types = claim.get("types") if isinstance(claim.get("types"), list) else []
        methods = claim.get("methods") if isinstance(claim.get("methods"), list) else []
        for check in checks:
            if not isinstance(check, dict):
                continue
            if depth == "core" and check.get("tier") != "core":
                errors.append("core plans must not contain extended checks")
                continue
            applicability = check.get("applies_to")
            if not isinstance(applicability, dict):
                continue
            required_types = applicability.get("claim_types")
            required_methods = applicability.get("methods")
            if not isinstance(required_types, list) or not isinstance(required_methods, list):
                continue
            if _applies(required_types, claim_types) and _applies(
                required_methods, methods
            ):
                expected_pairs.append((str(claim.get("id")), str(check.get("id"))))
    if actual_pairs != expected_pairs:
        errors.append(
            "tasks must equal every applicable claim/check pair exactly once and in order"
        )
    referenced_checks = {str(pair[1]) for pair in actual_pairs}
    if referenced_checks != set(check_by_id):
        errors.append("checks must contain exactly the check definitions used by tasks")
    return errors


def validate_check_plan_against_library(
    plan: object,
    library: object,
    *,
    library_sha256: str,
) -> list[str]:
    errors = validate_check_plan(plan)
    errors.extend(validate_check_library(library))
    if not _is_digest(library_sha256):
        errors.append("library_sha256 must be a lowercase SHA-256 digest")
    if errors:
        return errors
    assert isinstance(plan, dict)
    assert isinstance(library, dict)
    source = plan["source"]
    if source["library_sha256"] != library_sha256:
        errors.append("source.library_sha256 does not match the supplied check library")
        return errors
    expected = build_check_plan(
        plan["argument_ir"],
        library,
        ir_sha256=source["ir_sha256"],
        library_sha256=library_sha256,
        depth=plan["depth"],
        review_scope=(
            str(plan["review_scope"]["kind"])
            if plan.get("schema_version") in {2, 3}
            else "all"
        ),
        claim_ids=(
            list(plan["review_scope"]["claim_ids"])
            if plan.get("schema_version") in {2, 3}
            else []
        ),
    )
    if plan != expected:
        errors.append(
            "check plan is not the deterministic output of its Argument IR and library"
        )
    return errors


def render_check_prompt(plan: object, *, plan_sha256: str) -> str:
    errors = validate_check_plan(plan)
    if not _is_digest(plan_sha256):
        errors.append("plan_sha256 must be a lowercase SHA-256 digest")
    if errors:
        raise ArgumentIRError("; ".join(errors))
    assert isinstance(plan, dict)
    plan_json = json.dumps(plan, ensure_ascii=False, indent=2)
    if plan["schema_version"] == 3:
        return (
            "# Argument IR directed review v3\n\n"
            "Return exactly one result for every task, in task order. Do not add, "
            "remove, merge, or reorder tasks. The program has already fixed the "
            "review scope.\n\n"
            "First choose execution_status. Use evaluated only when the check applies "
            "and the supplied IR has enough context. Otherwise use "
            "blocked_missing_context, routing_mismatch, or not_applicable, give a "
            "specific reason and basis_refs, set verdict to null, and leave "
            "support_refs/support_paths empty. These statuses are audited and require "
            "human triage.\n\n"
            "For evaluated tasks, verdict is pass, fail, or uncertain. basis_refs are "
            "the IR nodes used to judge the task and may include the target Claim. "
            "support_refs are independent nodes that actually support PASS. Every "
            "support_ref must have one support_paths entry whose relation_ids form a "
            "directed path from that node to the target Claim. Only supports, qualifies, "
            "and cites relations may occur on a PASS support path; contradicts and "
            "assumes never establish PASS support. citation-required checks need a "
            "Citation support_ref whose path begins with cites. fail/uncertain must "
            "leave support_refs/support_paths empty. Use only the supplied IR.\n\n"
            "Use top-level status=complete only when every task is present; otherwise "
            "use partial and explain omitted work in unverified. Output pure JSON:\n\n"
            '{"schema_version":3,"artifact":"argument-check-results",'
            f'"source":{{"plan_sha256":"{plan_sha256}"}},'
            '"status":"complete","unverified":[],"results":['
            '{"task_id":"T1","execution_status":"evaluated",'
            '"verdict":"pass|fail|uncertain|null","reason":"...",'
            '"basis_refs":["C1"],"support_refs":["E1"],'
            '"support_paths":[{"support_ref":"E1","relation_ids":["R1"]}],'
            '"consequence":"required for fail/uncertain; otherwise empty"}]}\n\n'
            "# Check plan\n\n"
            f"{plan_json}\n"
        )
    if plan["schema_version"] == 2:
        return (
            "# Argument IR 定向审查 v2\n\n"
            "你不是自由发挥的通用 critic。逐项回答 check plan，不增加、删除、合并或重排任务。"
            "review_scope 已由程序确定；不要审查范围外的 Claim。\n\n"
            "先判断 execution_status：evaluated 表示检查适用且上下文足够；"
            "blocked_missing_context 表示缺少明确材料；routing_mismatch 表示 IR 分类或路由错误；"
            "not_applicable 表示该检查虽被路由到此处，但根据 Claim 实义确实不适用。"
            "后三类必须给出具体 reason 和 basis_refs，verdict 必须为 null，不能用来静默跳过任务。\n\n"
            "仅当 execution_status=evaluated 时填写 verdict=pass|fail|uncertain。"
            "basis_refs 说明判断依据，可包含目标 Claim；support_refs 只表示足以支持 PASS 的独立上游依据。"
            "每条 check 的 evidence_policy 决定 PASS 门槛：claim-text-sufficient 允许主张文本本身；"
            "upstream-required 必须给出目标 Claim 之外的上游节点；citation-required 必须包含 Citation。"
            "fail/uncertain 的 support_refs 必须为空。只使用 Argument IR 内材料，不凭外部记忆补全。\n\n"
            "正常完成全部任务写顶层 status=complete；若执行中断，保留原顺序写 status=partial，"
            "并在 unverified 说明未完成部分。只输出一个 JSON 对象，不要 Markdown 围栏。格式：\n\n"
            '{"schema_version":2,"artifact":"argument-check-results",'
            f'"source":{{"plan_sha256":"{plan_sha256}"}},'
            '"status":"complete","unverified":[],"results":['
            '{"task_id":"T1","execution_status":"evaluated",'
            '"verdict":"pass|fail|uncertain|null","reason":"...",'
            '"basis_refs":["C1"],"support_refs":["E1"],'
            '"consequence":"fail/uncertain 时说明影响，否则留空"}]}\n\n'
            "# Check plan\n\n"
            f"{plan_json}\n"
        )
    return (
        "# Argument IR 定向审查\n\n"
        "你不是自由发挥的通用 critic。下面的 check plan 已由程序根据主张类型和方法确定。"
        "逐项回答，不增加、删除、合并或重排任务，不改写 Claim 或检查标准。"
        "每个 task 只保存 claim_id/check_id：到 argument_ir.claims 和 checks 中按 ID 取定义，"
        "再沿 argument_ir.relations 反向追踪与该 Claim 相连的证据、假设、引用和前置主张。\n\n"
        "只使用 argument_ir 中的材料。缺少判断所需材料时写 uncertain，不得凭外部记忆补全。"
        "pass 表示没有触发 failure_condition；fail 表示已触发。若你认为 IR 分类错误，也必须写 uncertain"
        "并解释应如何重新抽取，不能跳过任务。\n\n"
        "pass/fail 的 evidence_refs 至少列出一个与该 Claim 处于同一反向关系链的节点 ID；"
        "uncertain 可在确实没有材料时留空。不要复制引文文本，程序会从节点 ID 确定性解析原文。"
        "正常情况下覆盖全部任务并写 status=complete；如果执行被中断，只按原顺序返回已完成任务，"
        "写 status=partial，并在 unverified 逐条说明未完成部分。\n\n"
        "只输出一个 JSON 对象，不要 Markdown 围栏。格式：\n\n"
        '{"schema_version":1,"artifact":"argument-check-results",'
        f'"source":{{"plan_sha256":"{plan_sha256}"}},'
        '"status":"complete","unverified":[],"results":['
        '{"task_id":"T1","verdict":"pass|fail|uncertain",'
        '"reason":"...","evidence_refs":["C1"],'
        '"consequence":"fail/uncertain 时说明对论证的影响，否则留空"}]}\n\n'
        "# Check plan\n\n"
        f"{plan_json}\n"
    )


def _context_node_ids(ir: dict[str, Any], claim_id: str) -> set[str]:
    reachable = {claim_id}
    changed = True
    while changed:
        changed = False
        for relation in ir["relations"]:
            if relation["to"] in reachable and relation["from"] not in reachable:
                reachable.add(relation["from"])
                changed = True
    return reachable


def _eligible_pass_support_paths(
    ir: dict[str, Any], claim_id: str
) -> dict[str, list[str]]:
    """Return deterministic directed support paths keyed by upstream node ID."""
    paths: dict[str, list[str]] = {claim_id: []}
    frontier = [claim_id]
    while frontier:
        target = frontier.pop(0)
        suffix = paths[target]
        for relation in ir["relations"]:
            if (
                relation["type"] not in PASS_SUPPORT_RELATION_TYPES
                or relation["to"] != target
            ):
                continue
            source = str(relation["from"])
            if source in paths:
                continue
            paths[source] = [str(relation["id"]), *suffix]
            frontier.append(source)
    paths.pop(claim_id, None)
    return paths


def validate_check_results(
    value: object,
    plan: object,
    *,
    plan_sha256: str,
) -> list[str]:
    errors = validate_check_plan(plan)
    if errors:
        return errors
    if not _is_digest(plan_sha256):
        return ["plan_sha256 must be a lowercase SHA-256 digest"]
    if not isinstance(value, dict):
        return ["check results must be a JSON object"]
    schema_version = value.get("schema_version")
    if set(value) != {
        "schema_version",
        "artifact",
        "source",
        "status",
        "unverified",
        "results",
    }:
        errors.append("check results must contain exactly the result envelope fields")
    if schema_version not in SUPPORTED_CHECK_RESULTS_SCHEMA_VERSIONS:
        errors.append("check results schema_version must be 1, 2, or 3")
    elif isinstance(plan, dict) and schema_version != plan.get("schema_version"):
        errors.append("check results schema_version must match the check plan")
    if value.get("artifact") != "argument-check-results":
        errors.append("check results artifact must be argument-check-results")
    source = value.get("source")
    if not isinstance(source, dict) or set(source) != {"plan_sha256"}:
        errors.append("check results source must contain exactly plan_sha256")
    else:
        _validate_digest(source.get("plan_sha256"), "source.plan_sha256", errors)
        if source.get("plan_sha256") != plan_sha256:
            errors.append("source.plan_sha256 does not match the supplied check plan")
    status = value.get("status")
    if status not in RESULT_STATUSES:
        errors.append("status must be complete or partial")
    unverified = _validate_string_list(
        value.get("unverified"), "unverified", errors, allow_empty=True
    )
    if status == "complete" and unverified:
        errors.append("complete results require an empty unverified array")
    if status == "partial" and not unverified:
        errors.append("partial results require concrete unverified entries")
    results = value.get("results")
    if not isinstance(results, list):
        errors.append("results must be an array")
        return errors
    assert isinstance(plan, dict)
    tasks = plan.get("tasks") if isinstance(plan.get("tasks"), list) else []
    task_by_id = {
        task["id"]: task
        for task in tasks
        if isinstance(task, dict) and isinstance(task.get("id"), str)
    }
    argument = plan["argument_ir"]
    nodes = _node_registry(argument)
    relation_by_id = {
        relation["id"]: relation
        for relation in argument["relations"]
        if isinstance(relation, dict) and isinstance(relation.get("id"), str)
    }
    check_by_id = {check["id"]: check for check in plan["checks"]}
    allowed_refs_by_claim = {
        claim["id"]: _context_node_ids(argument, claim["id"])
        for claim in argument["claims"]
    }
    expected_ids = list(task_by_id)
    actual_ids: list[object] = []
    for index, result in enumerate(results):
        label = f"results[{index}]"
        if not isinstance(result, dict):
            errors.append(f"{label} must be an object")
            continue
        expected_result_keys = (
            RESULT_KEYS_V3
            if schema_version == 3
            else RESULT_KEYS
            if schema_version == 2
            else RESULT_KEYS_V1
        )
        if set(result) != expected_result_keys:
            errors.append(
                f"{label} must contain exactly the v{schema_version} result fields"
            )
        task_id = result.get("task_id")
        actual_ids.append(task_id)
        task = task_by_id.get(str(task_id))
        if task is None:
            errors.append(f"{label}.task_id is not in the check plan: {task_id!r}")
        if not _nonempty_string(result.get("reason")):
            errors.append(f"{label}.reason must be a non-empty string")
        verdict = result.get("verdict")
        consequence = result.get("consequence")
        if schema_version == 1:
            if verdict not in RESULT_VERDICTS:
                errors.append(f"{label}.verdict must be one of {RESULT_VERDICTS}")
            basis_refs = _validate_string_list(
                result.get("evidence_refs"),
                label + ".evidence_refs",
                errors,
                allow_empty=True,
            )
            support_refs: list[str] = []
            execution_status = "evaluated"
            if verdict in {"pass", "fail"} and not basis_refs:
                errors.append(f"{label}.evidence_refs is required for {verdict}")
        else:
            execution_status = result.get("execution_status")
            if execution_status not in RESULT_EXECUTION_STATUSES:
                errors.append(
                    f"{label}.execution_status must be one of "
                    f"{RESULT_EXECUTION_STATUSES}"
                )
            if execution_status == "evaluated":
                if verdict not in RESULT_VERDICTS:
                    errors.append(
                        f"{label}.verdict must be one of {RESULT_VERDICTS} when evaluated"
                    )
            elif verdict is not None:
                errors.append(
                    f"{label}.verdict must be null unless execution_status=evaluated"
                )
            basis_refs = _validate_string_list(
                result.get("basis_refs"),
                label + ".basis_refs",
                errors,
                allow_empty=False,
            )
            support_refs = _validate_string_list(
                result.get("support_refs"),
                label + ".support_refs",
                errors,
                allow_empty=True,
            )
            if execution_status != "evaluated" and support_refs:
                errors.append(
                    f"{label}.support_refs must be empty when the check is not evaluated"
                )
            if execution_status == "evaluated" and verdict != "pass" and support_refs:
                errors.append(
                    f"{label}.support_refs is reserved for evidence supporting PASS"
                )
        support_paths: list[object] = []
        if schema_version == 3:
            raw_support_paths = result.get("support_paths")
            if not isinstance(raw_support_paths, list):
                errors.append(f"{label}.support_paths must be an array")
            else:
                support_paths = raw_support_paths
            if execution_status != "evaluated" and support_paths:
                errors.append(
                    f"{label}.support_paths must be empty when the check is not evaluated"
                )
            if execution_status == "evaluated" and verdict != "pass" and support_paths:
                errors.append(
                    f"{label}.support_paths is reserved for evidence supporting PASS"
                )
        if not isinstance(consequence, str):
            errors.append(f"{label}.consequence must be a string")
        elif execution_status == "evaluated" and verdict in {"fail", "uncertain"}:
            if not consequence.strip():
                errors.append(f"{label}.consequence is required for {verdict}")
        elif consequence.strip():
            errors.append(
                f"{label}.consequence must be empty unless verdict is fail or uncertain"
            )
        if task is not None:
            allowed_refs = allowed_refs_by_claim.get(task["claim_id"], set())
            unknown_refs = [
                reference
                for reference in basis_refs + support_refs
                if reference not in nodes or reference not in allowed_refs
            ]
            if unknown_refs:
                errors.append(
                    f"{label} contains nodes outside the claim context: {unknown_refs}"
                )
            if schema_version in {2, 3} and execution_status == "evaluated" and verdict == "pass":
                check = check_by_id.get(task["check_id"], {})
                policy = check.get("evidence_policy")
                if task["claim_id"] in support_refs:
                    errors.append(
                        f"{label}.support_refs must not contain the target Claim; "
                        "use basis_refs for claim text"
                    )
                if policy == "upstream-required":
                    independent = [
                        reference
                        for reference in support_refs
                        if reference != task["claim_id"]
                    ]
                    if not independent:
                        errors.append(
                            f"{label}.support_refs requires an upstream node independent "
                            "of the target Claim"
                        )
                elif policy == "citation-required":
                    if not any(
                        reference in nodes and nodes[reference][0] == "citation"
                        for reference in support_refs
                    ):
                        errors.append(
                            f"{label}.support_refs requires a Citation for this check"
                        )
                if schema_version == 3:
                    path_refs: list[object] = []
                    for path_index, support_path in enumerate(support_paths):
                        path_label = f"{label}.support_paths[{path_index}]"
                        if not isinstance(support_path, dict):
                            errors.append(f"{path_label} must be an object")
                            continue
                        if set(support_path) != SUPPORT_PATH_KEYS:
                            errors.append(
                                f"{path_label} must contain support_ref and relation_ids"
                            )
                        support_ref = support_path.get("support_ref")
                        path_refs.append(support_ref)
                        if not _nonempty_string(support_ref):
                            errors.append(f"{path_label}.support_ref must be non-empty")
                        relation_ids = _validate_string_list(
                            support_path.get("relation_ids"),
                            f"{path_label}.relation_ids",
                            errors,
                            allow_empty=False,
                        )
                        current = support_ref
                        used: set[str] = set()
                        first_relation_type: str | None = None
                        for relation_id in relation_ids:
                            relation = relation_by_id.get(relation_id)
                            if relation is None:
                                errors.append(
                                    f"{path_label} references unknown relation: {relation_id!r}"
                                )
                                continue
                            if relation_id in used:
                                errors.append(
                                    f"{path_label}.relation_ids must not repeat relations"
                                )
                            used.add(relation_id)
                            relation_type = str(relation["type"])
                            if first_relation_type is None:
                                first_relation_type = relation_type
                            if relation_type not in PASS_SUPPORT_RELATION_TYPES:
                                errors.append(
                                    f"{path_label} uses {relation_type}, which cannot support PASS"
                                )
                            if relation["from"] != current:
                                errors.append(
                                    f"{path_label} is not a directed path from {support_ref}"
                                )
                            current = relation["to"]
                        if current != task["claim_id"]:
                            errors.append(
                                f"{path_label} must end at target Claim {task['claim_id']}"
                            )
                        if (
                            policy == "citation-required"
                            and isinstance(support_ref, str)
                            and support_ref in nodes
                            and nodes[support_ref][0] == "citation"
                            and first_relation_type != "cites"
                        ):
                            errors.append(
                                f"{path_label} for a Citation must begin with cites"
                            )
                    if path_refs != support_refs:
                        errors.append(
                            f"{label}.support_paths must map one-to-one, in order, to support_refs"
                        )
    if len(actual_ids) != len(set(actual_ids)):
        errors.append("results must not repeat task IDs")
    if status == "complete" and actual_ids != expected_ids:
        errors.append("complete results must cover every task exactly once and in order")
    if status == "partial":
        expected_subsequence = [task_id for task_id in expected_ids if task_id in actual_ids]
        if actual_ids != expected_subsequence:
            errors.append("partial results must follow check-plan task order")
    return errors


def build_argument_findings(
    plan: object,
    results: object,
    *,
    plan_sha256: str,
    results_sha256: str,
) -> dict[str, Any]:
    errors = validate_check_results(results, plan, plan_sha256=plan_sha256)
    if not _is_digest(results_sha256):
        errors.append("results_sha256 must be a lowercase SHA-256 digest")
    if errors:
        raise ArgumentIRError("; ".join(errors))
    assert isinstance(plan, dict)
    assert isinstance(results, dict)
    task_by_id = {task["id"]: task for task in plan["tasks"]}
    argument = plan["argument_ir"]
    claim_by_id = {claim["id"]: claim for claim in argument["claims"]}
    nodes = _node_registry(argument)
    findings: list[dict[str, Any]] = []
    results_version = int(results["schema_version"])
    for result in results["results"]:
        if results_version in {2, 3} and result["execution_status"] != "evaluated":
            continue
        if result["verdict"] not in {"fail", "uncertain"}:
            continue
        task = task_by_id[result["task_id"]]
        claim = claim_by_id[task["claim_id"]]
        resolved_evidence = []
        evidence_refs = (
            result["basis_refs"]
            if results_version in {2, 3}
            else result["evidence_refs"]
        )
        for reference in evidence_refs:
            node_kind, node = nodes[reference]
            resolved_evidence.append(
                {
                    "node_id": reference,
                    "node_kind": node_kind,
                    "text": node["text"],
                    "source_quote": node["source_quote"],
                    "position": node["position"],
                }
            )
        findings.append(
            {
                "id": f"F{len(findings) + 1}",
                "task_id": result["task_id"],
                "claim_id": task["claim_id"],
                "check_id": task["check_id"],
                "verdict": result["verdict"],
                "claim_text": claim["text"],
                "position": claim["position"],
                "reason": result["reason"],
                "consequence": result["consequence"],
                "evidence_refs": list(evidence_refs),
                "evidence": resolved_evidence,
            }
        )
    return {
        "schema_version": ARGUMENT_FINDINGS_SCHEMA_VERSION,
        "artifact": "argument-findings",
        "source": {
            "plan_sha256": plan_sha256,
            "results_sha256": results_sha256,
        },
        "findings": findings,
    }


def validate_argument_findings(value: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["argument findings must be a JSON object"]
    if set(value) != {"schema_version", "artifact", "source", "findings"}:
        errors.append("argument findings must contain exactly the v1 fields")
    if value.get("schema_version") != ARGUMENT_FINDINGS_SCHEMA_VERSION:
        errors.append("argument findings schema_version must be 1")
    if value.get("artifact") != "argument-findings":
        errors.append("argument findings artifact must be argument-findings")
    source = value.get("source")
    if not isinstance(source, dict) or set(source) != {
        "plan_sha256",
        "results_sha256",
    }:
        errors.append("argument findings source fields are invalid")
    else:
        _validate_digest(source.get("plan_sha256"), "source.plan_sha256", errors)
        _validate_digest(source.get("results_sha256"), "source.results_sha256", errors)
    findings = _validate_continuous_ids(
        value.get("findings"), "findings", "F", errors
    )
    for index, finding in enumerate(findings):
        label = f"findings[{index}]"
        if set(finding) != FINDING_KEYS:
            errors.append(f"{label} must contain exactly the finding fields")
        for key in (
            "task_id",
            "claim_id",
            "check_id",
            "claim_text",
            "position",
            "reason",
            "consequence",
        ):
            if not _nonempty_string(finding.get(key)):
                errors.append(f"{label}.{key} must be a non-empty string")
        if finding.get("verdict") not in {"fail", "uncertain"}:
            errors.append(f"{label}.verdict must be fail or uncertain")
        evidence_refs = _validate_string_list(
            finding.get("evidence_refs"),
            label + ".evidence_refs",
            errors,
            allow_empty=True,
        )
        evidence = finding.get("evidence")
        if not isinstance(evidence, list):
            errors.append(f"{label}.evidence must be an array")
            continue
        evidence_ids: list[object] = []
        for evidence_index, item in enumerate(evidence):
            evidence_label = f"{label}.evidence[{evidence_index}]"
            if not isinstance(item, dict) or set(item) != FINDING_EVIDENCE_KEYS:
                errors.append(f"{evidence_label} has invalid fields")
                continue
            evidence_ids.append(item.get("node_id"))
            if item.get("node_kind") not in {
                "claim",
                "evidence",
                "assumption",
                "citation",
            }:
                errors.append(f"{evidence_label}.node_kind is invalid")
            for key in ("node_id", "text", "source_quote", "position"):
                if not _nonempty_string(item.get(key)):
                    errors.append(f"{evidence_label}.{key} must be a non-empty string")
        if evidence_ids != evidence_refs:
            errors.append(f"{label}.evidence must resolve evidence_refs in order")
    return errors


def build_ir_extraction_prompt(
    manuscript: str,
    *,
    source_name: str,
    source_sha256: str,
    protocol_version: int = IR_EXTRACTION_PROTOCOL_VERSION,
) -> str:
    if not manuscript.strip():
        raise ArgumentIRError("manuscript must not be empty")
    if not _safe_basename(source_name):
        raise ArgumentIRError("source_name must be a safe basename")
    if not _is_digest(source_sha256):
        raise ArgumentIRError("source_sha256 must be a lowercase SHA-256 digest")
    if protocol_version not in SUPPORTED_IR_EXTRACTION_PROTOCOL_VERSIONS:
        raise ArgumentIRError(
            "protocol_version must identify a supported IR extraction protocol"
        )
    protocol_header = (
        ""
        if protocol_version == 1
        else "Protocol: argument-ir-extraction-v2\n\n"
    )
    extraction_guidance = (
        "把下面稿件转换成 JSON Argument IR。你只做结构抽取，不评价主张质量，不运行 critic。"
        "承担推理功能的复合句要拆成可独立判断的 Claim；每个节点必须保留稿件中的逐字 source_quote 和位置。"
        if protocol_version == 1
        else (
            "把下面稿件转换成 JSON Argument IR。你只做结构抽取，不评价主张质量，不运行 critic。"
            "只抽取承担论证功能的重要 Claim，不要把标题、修辞、过渡句、研究过程叙述或作者明确反对的命题误当成作者主张。"
            "承担多个独立推理功能的复合句要拆成可独立判断的 Claim；每个节点必须保留稿件中的逐字 source_quote 和位置。"
            "每条 Claim 的 types 和 methods 默认各选择一个最主要值。只有同一个不可再拆的 Claim 确实独立承担多个功能时才使用多个值，"
            "并在 uncertainty 说明为什么不能拆开。methods 描述实际支撑该 Claim 的方法，不是列出整篇文章使用过的所有方法。"
            "不要仅因主张谈到一般趋势或未来后果就同时标为 predictive；predictive 应保留给具有明确预测对象和验证条件的主张。"
            "每个 conclusion 或 intermediate Claim 应尽可能有可追踪的 supports/qualifies 入边；"
            "不要用 Claim 自身的重复表述冒充 Evidence。若原稿确实没有可定位支持，把缺口写入 unverified，不要虚构节点。"
        )
    )
    return (
        "# Argument IR extraction\n\n"
        f"{protocol_header}"
        f"{extraction_guidance}"
        "source_quote 必须足够长，使它在整篇稿件中只出现一次；position 先写人可读提示，"
        "程序随后会按唯一引文确定性改写为行列区间。"
        "不得补充稿件外事实。隐含 Claim/Assumption 标为 inferred，并在 uncertainty 解释推断依据；"
        "不要输出数值 confidence。\n\n"
        f"Claim types: {', '.join(CLAIM_TYPES)}\n"
        f"Methods: {', '.join(METHOD_TYPES)}\n"
        f"Relations: {', '.join(RELATION_TYPES)}\n\n"
        "只输出一个 JSON 对象，不要 Markdown 围栏。顶层必须严格包含："
        "schema_version, artifact, scope, source, claims, evidence, assumptions, citations, relations, unverified。"
        "ID 分别连续使用 C1/E1/A1/Z1/R1。scope 固定为 social-science。\n\n"
        "严格字段模板如下；不要增加字段。示例中的省略文本必须替换为原稿内容，"
        "没有某类节点时使用空数组：\n"
        '{"schema_version":1,"artifact":"argument-ir","scope":"social-science",'
        f'"source":{{"name":{json.dumps(source_name, ensure_ascii=False)},'
        f'"sha256":"{source_sha256}"}},'
        '"claims":[{"id":"C1","text":"...","source_quote":"...","position":"...",'
        '"types":["conceptual"],"methods":["conceptual-analysis"],'
        '"role":"conclusion","extraction":"explicit","uncertainty":""}],'
        '"evidence":[{"id":"E1","text":"...","source_quote":"...",'
        '"position":"...","kind":"data"}],'
        '"assumptions":[{"id":"A1","text":"...","source_quote":"...",'
        '"position":"...","extraction":"inferred","uncertainty":"..."}],'
        '"citations":[{"id":"Z1","text":"...","source_quote":"...",'
        '"position":"...","locator":""}],'
        '"relations":[{"id":"R1","type":"supports","from":"E1","to":"C1"}],'
        '"unverified":[]}\n\n'
        f"Evidence kinds: {', '.join(EVIDENCE_KINDS)}\n"
        f"Claim roles: {', '.join(CLAIM_ROLES)}\n"
        "关系端点规则：supports/contradicts/qualifies 的目标必须是 Claim；"
        "assumes 必须由 Assumption 指向 Claim；cites 必须由 Citation 指向 Claim 或 Evidence。\n\n"
        "# Manuscript\n\n"
        f"{manuscript.rstrip()}\n"
    )
