"""Machine-readable contracts for argument structure and method-conditional review."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any


ARGUMENT_IR_SCHEMA_VERSION = 1
CHECK_LIBRARY_SCHEMA_VERSION = 1
CHECK_PLAN_SCHEMA_VERSION = 1
CHECK_RESULTS_SCHEMA_VERSION = 1
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
RESULT_VERDICTS = ("pass", "fail", "uncertain", "not_applicable")
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
CHECK_KEYS = {
    "id",
    "label",
    "category",
    "tier",
    "applies_to",
    "question",
    "failure_condition",
    "required_context",
}
TASK_KEYS = {
    "id",
    "claim_id",
    "check_id",
    "category",
    "tier",
    "question",
    "failure_condition",
    "required_context",
    "context",
}
TASK_CONTEXT_KEYS = {"claim", "incoming"}
INCOMING_KEYS = {
    "relation_id",
    "relation_type",
    "node_id",
    "node_kind",
    "target_id",
    "text",
    "source_quote",
    "position",
    "details",
}
RESULT_KEYS = {"task_id", "verdict", "reason", "evidence", "consequence"}
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
    "evidence",
}

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_CHECK_ID_PATTERN = re.compile(r"[a-z][a-z0-9-]*(?:\.[a-z][a-z0-9-]*)+\Z")


class ArgumentIRError(ValueError):
    """Raised when an IR artifact cannot be derived safely."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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


def _validate_digest(value: object, label: str, errors: list[str]) -> None:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        errors.append(f"{label} must be a lowercase SHA-256 digest")


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
    if manuscript_text is not None and isinstance(quote, str) and quote not in manuscript_text:
        errors.append(f"{label}.source_quote is not an exact substring of the source manuscript")


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

    _validate_string_list(
        value.get("unverified"), "unverified", errors, allow_empty=True
    )
    return errors


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
    if value.get("schema_version") != CHECK_LIBRARY_SCHEMA_VERSION:
        errors.append("check library schema_version must be 1")
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
        if set(check) != CHECK_KEYS:
            errors.append(f"{label} must contain exactly the check fields")
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


def _applies(required: list[str], actual: list[str]) -> bool:
    return required == ["*"] or bool(set(required) & set(actual))


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


def _node_details(kind: str, node: dict[str, Any]) -> dict[str, Any]:
    if kind == "claim":
        return {
            key: node[key]
            for key in ("types", "methods", "role", "extraction", "uncertainty")
        }
    if kind == "evidence":
        return {"kind": node["kind"]}
    if kind == "assumption":
        return {
            key: node[key] for key in ("extraction", "uncertainty")
        }
    return {"locator": node["locator"]}


def build_check_plan(
    ir: object,
    library: object,
    *,
    ir_sha256: str,
    library_sha256: str,
    depth: str,
) -> dict[str, Any]:
    errors = validate_argument_ir(ir)
    errors.extend(validate_check_library(library))
    if depth not in CHECK_DEPTHS:
        errors.append("depth must be core or full")
    if _SHA256_PATTERN.fullmatch(ir_sha256) is None:
        errors.append("ir_sha256 must be a lowercase SHA-256 digest")
    if _SHA256_PATTERN.fullmatch(library_sha256) is None:
        errors.append("library_sha256 must be a lowercase SHA-256 digest")
    if errors:
        raise ArgumentIRError("; ".join(errors))
    assert isinstance(ir, dict)
    assert isinstance(library, dict)
    nodes = _node_registry(ir)
    incoming_by_claim: dict[str, list[dict[str, Any]]] = {}
    for claim in ir["claims"]:
        claim_id = claim["id"]
        direct = [relation for relation in ir["relations"] if relation["to"] == claim_id]
        direct_node_ids = {relation["from"] for relation in direct}
        citation_context = [
            relation
            for relation in ir["relations"]
            if relation["type"] == "cites" and relation["to"] in direct_node_ids
        ]
        relevant = direct + citation_context
        incoming_by_claim[claim_id] = []
        for relation in relevant:
            node_kind, node = nodes[relation["from"]]
            incoming_by_claim[claim_id].append(
                {
                    "relation_id": relation["id"],
                    "relation_type": relation["type"],
                    "node_id": relation["from"],
                    "node_kind": node_kind,
                    "target_id": relation["to"],
                    "text": node["text"],
                    "source_quote": node["source_quote"],
                    "position": node["position"],
                    "details": _node_details(node_kind, node),
                }
            )

    tasks: list[dict[str, Any]] = []
    for claim in ir["claims"]:
        for check in library["checks"]:
            if depth == "core" and check["tier"] != "core":
                continue
            applicability = check["applies_to"]
            if not _applies(applicability["claim_types"], claim["types"]):
                continue
            if not _applies(applicability["methods"], claim["methods"]):
                continue
            tasks.append(
                {
                    "id": f"T{len(tasks) + 1}",
                    "claim_id": claim["id"],
                    "check_id": check["id"],
                    "category": check["category"],
                    "tier": check["tier"],
                    "question": check["question"],
                    "failure_condition": check["failure_condition"],
                    "required_context": list(check["required_context"]),
                    "context": {
                        "claim": dict(claim),
                        "incoming": list(incoming_by_claim[claim["id"]]),
                    },
                }
            )
    return {
        "schema_version": CHECK_PLAN_SCHEMA_VERSION,
        "artifact": "argument-check-plan",
        "depth": depth,
        "source": {
            "ir_sha256": ir_sha256,
            "library_sha256": library_sha256,
            "scope": ir["scope"],
        },
        "tasks": tasks,
    }


def validate_check_plan(value: object) -> list[str]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return ["check plan must be a JSON object"]
    if set(value) != {"schema_version", "artifact", "depth", "source", "tasks"}:
        errors.append("check plan must contain exactly the v1 fields")
    if value.get("schema_version") != CHECK_PLAN_SCHEMA_VERSION:
        errors.append("check plan schema_version must be 1")
    if value.get("artifact") != "argument-check-plan":
        errors.append("check plan artifact must be argument-check-plan")
    if value.get("depth") not in CHECK_DEPTHS:
        errors.append("check plan depth must be core or full")
    source = value.get("source")
    if not isinstance(source, dict) or set(source) != {
        "ir_sha256",
        "library_sha256",
        "scope",
    }:
        errors.append("check plan source must contain IR hash, library hash, and scope")
    else:
        _validate_digest(source.get("ir_sha256"), "source.ir_sha256", errors)
        _validate_digest(source.get("library_sha256"), "source.library_sha256", errors)
        if source.get("scope") != "social-science":
            errors.append("check plan source.scope must be social-science")
    tasks = _validate_continuous_ids(value.get("tasks"), "tasks", "T", errors)
    seen_pairs: set[tuple[object, object]] = set()
    for index, task in enumerate(tasks):
        label = f"tasks[{index}]"
        if set(task) != TASK_KEYS:
            errors.append(f"{label} must contain exactly the task fields")
        if not isinstance(task.get("claim_id"), str) or not re.fullmatch(
            r"C[1-9][0-9]*", task["claim_id"]
        ):
            errors.append(f"{label}.claim_id must be C1..Cn")
        if not isinstance(task.get("check_id"), str) or _CHECK_ID_PATTERN.fullmatch(
            task["check_id"]
        ) is None:
            errors.append(f"{label}.check_id is invalid")
        for key in ("category", "question", "failure_condition"):
            if not _nonempty_string(task.get(key)):
                errors.append(f"{label}.{key} must be a non-empty string")
        if task.get("tier") not in CHECK_TIERS:
            errors.append(f"{label}.tier must be core or extended")
        _validate_string_list(
            task.get("required_context"),
            label + ".required_context",
            errors,
            allowed=REQUIRED_CONTEXT_VALUES,
            allow_empty=False,
        )
        context = task.get("context")
        if not isinstance(context, dict) or set(context) != TASK_CONTEXT_KEYS:
            errors.append(f"{label}.context must contain claim and incoming")
        else:
            claim = context.get("claim")
            if not isinstance(claim, dict) or set(claim) != CLAIM_KEYS:
                errors.append(f"{label}.context.claim is invalid")
            else:
                if claim.get("id") != task.get("claim_id"):
                    errors.append(f"{label}.context.claim.id does not match claim_id")
                _validate_provenance_fields(claim, label + ".context.claim", errors, None)
                _validate_string_list(
                    claim.get("types"),
                    label + ".context.claim.types",
                    errors,
                    allowed=CLAIM_TYPES,
                    allow_empty=False,
                )
                methods = _validate_string_list(
                    claim.get("methods"),
                    label + ".context.claim.methods",
                    errors,
                    allowed=METHOD_TYPES,
                    allow_empty=False,
                )
                if len(methods) > 1 and ({"unspecified", "other"} & set(methods)):
                    errors.append(
                        f"{label}.context.claim.methods must use unspecified or other alone"
                    )
                if claim.get("role") not in CLAIM_ROLES:
                    errors.append(f"{label}.context.claim.role is invalid")
                if claim.get("extraction") not in EXTRACTION_MODES:
                    errors.append(f"{label}.context.claim.extraction is invalid")
                if not isinstance(claim.get("uncertainty"), str):
                    errors.append(f"{label}.context.claim.uncertainty must be a string")
            incoming = context.get("incoming")
            if not isinstance(incoming, list):
                errors.append(f"{label}.context.incoming must be an array")
            else:
                valid_incoming: list[dict[str, Any]] = []
                for incoming_index, item in enumerate(incoming):
                    incoming_label = f"{label}.context.incoming[{incoming_index}]"
                    if not isinstance(item, dict) or set(item) != INCOMING_KEYS:
                        errors.append(f"{incoming_label} has invalid fields")
                    elif any(
                        not _nonempty_string(item.get(key))
                        for key in INCOMING_KEYS - {"details"}
                    ):
                        errors.append(f"{incoming_label} fields must be non-empty strings")
                    else:
                        valid_incoming.append(item)
                        if item.get("relation_type") not in RELATION_TYPES:
                            errors.append(f"{incoming_label}.relation_type is invalid")
                        node_kind = item.get("node_kind")
                        if node_kind not in {
                            "claim",
                            "evidence",
                            "assumption",
                            "citation",
                        }:
                            errors.append(f"{incoming_label}.node_kind is invalid")
                        expected_prefix = {
                            "claim": "C",
                            "evidence": "E",
                            "assumption": "A",
                            "citation": "Z",
                        }.get(str(node_kind))
                        if expected_prefix is not None and re.fullmatch(
                            rf"{expected_prefix}[1-9][0-9]*", str(item.get("node_id"))
                        ) is None:
                            errors.append(
                                f"{incoming_label}.node_id does not match node_kind"
                            )
                        if re.fullmatch(
                            r"[CE][1-9][0-9]*", str(item.get("target_id"))
                        ) is None:
                            errors.append(f"{incoming_label}.target_id is invalid")
                        details = item.get("details")
                        expected_detail_keys = {
                            "claim": {
                                "types",
                                "methods",
                                "role",
                                "extraction",
                                "uncertainty",
                            },
                            "evidence": {"kind"},
                            "assumption": {"extraction", "uncertainty"},
                            "citation": {"locator"},
                        }.get(str(node_kind))
                        if (
                            not isinstance(details, dict)
                            or expected_detail_keys is None
                            or set(details) != expected_detail_keys
                        ):
                            errors.append(
                                f"{incoming_label}.details does not match node_kind"
                            )
                        elif node_kind == "claim":
                            _validate_string_list(
                                details.get("types"),
                                incoming_label + ".details.types",
                                errors,
                                allowed=CLAIM_TYPES,
                                allow_empty=False,
                            )
                            _validate_string_list(
                                details.get("methods"),
                                incoming_label + ".details.methods",
                                errors,
                                allowed=METHOD_TYPES,
                                allow_empty=False,
                            )
                            if details.get("role") not in CLAIM_ROLES:
                                errors.append(f"{incoming_label}.details.role is invalid")
                            if details.get("extraction") not in EXTRACTION_MODES:
                                errors.append(
                                    f"{incoming_label}.details.extraction is invalid"
                                )
                            if not isinstance(details.get("uncertainty"), str):
                                errors.append(
                                    f"{incoming_label}.details.uncertainty must be a string"
                                )
                        elif node_kind == "evidence":
                            if details.get("kind") not in EVIDENCE_KINDS:
                                errors.append(f"{incoming_label}.details.kind is invalid")
                        elif node_kind == "assumption":
                            if details.get("extraction") not in EXTRACTION_MODES:
                                errors.append(
                                    f"{incoming_label}.details.extraction is invalid"
                                )
                            if not isinstance(details.get("uncertainty"), str):
                                errors.append(
                                    f"{incoming_label}.details.uncertainty must be a string"
                                )
                        elif node_kind == "citation" and not isinstance(
                            details.get("locator"), str
                        ):
                            errors.append(
                                f"{incoming_label}.details.locator must be a string"
                            )
                direct_node_ids = {
                    item["node_id"]
                    for item in valid_incoming
                    if item["relation_type"] != "cites"
                    and item["target_id"] == task.get("claim_id")
                }
                allowed_source_kinds = {
                    "supports": {"claim", "evidence"},
                    "contradicts": {"claim", "evidence"},
                    "qualifies": {"claim", "evidence"},
                    "assumes": {"assumption"},
                    "cites": {"citation"},
                }
                for incoming_index, item in enumerate(valid_incoming):
                    incoming_label = f"{label}.context.incoming[{incoming_index}]"
                    relation_type = item["relation_type"]
                    allowed_kinds = allowed_source_kinds.get(relation_type, set())
                    if item["node_kind"] not in allowed_kinds:
                        errors.append(
                            f"{incoming_label}.node_kind is invalid for {relation_type}"
                        )
                    if relation_type == "cites":
                        if item["target_id"] not in {
                            task.get("claim_id"),
                            *direct_node_ids,
                        }:
                            errors.append(
                                f"{incoming_label}.target_id is outside the claim context"
                            )
                    elif item["target_id"] != task.get("claim_id"):
                        errors.append(
                            f"{incoming_label}.target_id must equal the task claim_id"
                        )
        pair = (task.get("claim_id"), task.get("check_id"))
        if pair in seen_pairs:
            errors.append(f"{label} duplicates a claim/check pair")
        seen_pairs.add(pair)
    return errors


def render_check_prompt(plan: object, *, plan_sha256: str) -> str:
    errors = validate_check_plan(plan)
    if _SHA256_PATTERN.fullmatch(plan_sha256) is None:
        errors.append("plan_sha256 must be a lowercase SHA-256 digest")
    if errors:
        raise ArgumentIRError("; ".join(errors))
    assert isinstance(plan, dict)
    plan_json = json.dumps(plan, ensure_ascii=False, indent=2)
    return (
        "# Argument IR 定向审查\n\n"
        "你不是自由发挥的通用 critic。下面的 check plan 已由程序根据主张类型和方法确定。"
        "逐项回答，不增加、删除、合并或重排任务，不改写 Claim 或检查标准。\n\n"
        "只使用 task.context 中的材料。缺少判断所需材料时写 uncertain，不得凭外部记忆补全。"
        "pass 表示没有触发 failure_condition；fail 表示已触发；not_applicable 只能用于 IR 分类明显错误。\n\n"
        "evidence 只能原样复制 context.claim.source_quote 或 context.incoming[*].source_quote 的完整值。"
        "正常情况下覆盖全部任务并写 status=complete；如果执行被中断，只按原顺序返回已完成任务，"
        "写 status=partial，并在 unverified 逐条说明未完成部分。\n\n"
        "只输出一个 JSON 对象，不要 Markdown 围栏。格式：\n\n"
        '{"schema_version":1,"artifact":"argument-check-results",'
        f'"source":{{"plan_sha256":"{plan_sha256}"}},'
        '"status":"complete","unverified":[],"results":['
        '{"task_id":"T1","verdict":"pass|fail|uncertain|not_applicable",'
        '"reason":"...","evidence":["必须逐字来自 task context 的引文"],'
        '"consequence":"fail/uncertain 时说明对论证的影响，否则留空"}]}\n\n'
        "# Check plan\n\n"
        f"{plan_json}\n"
    )


def validate_check_results(
    value: object,
    plan: object,
    *,
    plan_sha256: str,
) -> list[str]:
    errors = validate_check_plan(plan)
    if errors:
        return errors
    if not isinstance(value, dict):
        return ["check results must be a JSON object"]
    if set(value) != {
        "schema_version",
        "artifact",
        "source",
        "status",
        "unverified",
        "results",
    }:
        errors.append("check results must contain exactly the v1 fields")
    if value.get("schema_version") != CHECK_RESULTS_SCHEMA_VERSION:
        errors.append("check results schema_version must be 1")
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
    expected_ids = list(task_by_id)
    actual_ids: list[object] = []
    for index, result in enumerate(results):
        label = f"results[{index}]"
        if not isinstance(result, dict):
            errors.append(f"{label} must be an object")
            continue
        if set(result) != RESULT_KEYS:
            errors.append(f"{label} must contain exactly the result fields")
        task_id = result.get("task_id")
        actual_ids.append(task_id)
        task = task_by_id.get(str(task_id))
        if task is None:
            errors.append(f"{label}.task_id is not in the check plan: {task_id!r}")
        verdict = result.get("verdict")
        if verdict not in RESULT_VERDICTS:
            errors.append(f"{label}.verdict must be one of {RESULT_VERDICTS}")
        if not _nonempty_string(result.get("reason")):
            errors.append(f"{label}.reason must be a non-empty string")
        evidence = _validate_string_list(
            result.get("evidence"), label + ".evidence", errors, allow_empty=True
        )
        consequence = result.get("consequence")
        if not isinstance(consequence, str):
            errors.append(f"{label}.consequence must be a string")
        elif verdict in {"fail", "uncertain"} and not consequence.strip():
            errors.append(f"{label}.consequence is required for {verdict}")
        elif verdict in {"pass", "not_applicable"} and consequence.strip():
            errors.append(f"{label}.consequence must be empty for {verdict}")
        if task is not None:
            allowed_quotes = {task["context"]["claim"]["source_quote"]}
            allowed_quotes.update(
                item["source_quote"] for item in task["context"]["incoming"]
            )
            unknown_quotes = [quote for quote in evidence if quote not in allowed_quotes]
            if unknown_quotes:
                errors.append(
                    f"{label}.evidence contains text not present in task context"
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
    if _SHA256_PATTERN.fullmatch(results_sha256) is None:
        errors.append("results_sha256 must be a lowercase SHA-256 digest")
    if errors:
        raise ArgumentIRError("; ".join(errors))
    assert isinstance(plan, dict)
    assert isinstance(results, dict)
    task_by_id = {task["id"]: task for task in plan["tasks"]}
    findings: list[dict[str, Any]] = []
    for result in results["results"]:
        if result["verdict"] not in {"fail", "uncertain"}:
            continue
        task = task_by_id[result["task_id"]]
        claim = task["context"]["claim"]
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
                "evidence": list(result["evidence"]),
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
        _validate_string_list(
            finding.get("evidence"), label + ".evidence", errors, allow_empty=True
        )
    return errors


def build_ir_extraction_prompt(
    manuscript: str,
    *,
    source_name: str,
    source_sha256: str,
) -> str:
    if not manuscript.strip():
        raise ArgumentIRError("manuscript must not be empty")
    if not _safe_basename(source_name):
        raise ArgumentIRError("source_name must be a safe basename")
    if _SHA256_PATTERN.fullmatch(source_sha256) is None:
        raise ArgumentIRError("source_sha256 must be a lowercase SHA-256 digest")
    return (
        "# Argument IR extraction\n\n"
        "把下面稿件转换成 JSON Argument IR。你只做结构抽取，不评价主张质量，不运行 critic。"
        "承担推理功能的复合句要拆成可独立判断的 Claim；每个节点必须保留稿件中的逐字 source_quote 和位置。"
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
