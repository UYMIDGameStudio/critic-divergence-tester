"""Deterministic W/B divergence scoring from human one-to-one pairings."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from itertools import combinations


WITHIN_COMPARISONS = ("I1:I2", "C1:C2")
BETWEEN_COMPARISONS = ("I1:C1", "I1:C2", "I2:C1", "I2:C2")
ALL_COMPARISONS = WITHIN_COMPARISONS + BETWEEN_COMPARISONS
RUN_NAMES = ("I1", "I2", "C1", "C2")
CLAIM_ID_PATTERN = re.compile(r"A[1-9][0-9]*")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
PAIR_CLASSIFICATIONS = {"overlap", "different_reason", "ambiguous"}
RUN_LABEL_PATTERN = re.compile(r"[^:\x00-\x1f\x7f]{1,128}")
BLIND_ALIAS_PATTERN = re.compile(r"R[0-9]{2,}")


class ScorecardError(ValueError):
    """Raised when a divergence scorecard cannot be reproduced safely."""


def scorecard_template() -> dict[str, object]:
    """Return the legacy aggregate-count template for detached/manual use."""
    blank = {
        "overlap": None,
        "different_reason": None,
        "left_unique": None,
        "right_unique": None,
        "ambiguous": None,
    }
    return {
        "schema_version": 1,
        "margin": 0.2,
        "instructions": (
            "Fill every count after one-to-one human pairing. Ambiguous is a paired "
            "item that could be overlap or different_reason."
        ),
        "comparisons": {name: dict(blank) for name in ALL_COMPARISONS},
    }


def pairing_scorecard(runs: dict[str, dict[str, object]]) -> dict[str, object]:
    """Create a traceable scorecard whose aggregate counts are derived from pairs."""
    return {
        "schema_version": 2,
        "margin": 0.2,
        "instructions": (
            "For every comparison, add one-to-one A-item pairs, set classification "
            "to overlap, different_reason, or ambiguous, then set complete=true. "
            "Unpaired claims are counted as unique automatically."
        ),
        "runs": runs,
        "comparisons": {
            name: {"complete": False, "pairs": []} for name in ALL_COMPARISONS
        },
    }


def campaign_pairing_scorecard(
    runs: dict[str, dict[str, object]],
) -> dict[str, object]:
    """Create a traceable scorecard for an arbitrary balanced campaign."""
    scorecard: dict[str, object] = {
        "schema_version": 3,
        "margin": 0.2,
        "instructions": (
            "For every comparison, add one-to-one A-item pairs, set classification "
            "to overlap, different_reason, or ambiguous, then set complete=true. "
            "Unpaired claims are counted as unique automatically."
        ),
        "run_order": list(runs),
        "runs": runs,
    }
    _, within, between = _dynamic_layout(scorecard)
    scorecard["comparisons"] = {
        name: {"complete": False, "pairs": []} for name in within + between
    }
    return scorecard


def _score_count(value: object, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ScorecardError(f"{path} must be a non-negative integer")
    return value


def _margin(scorecard: dict[str, object]) -> float:
    raw_margin = scorecard.get("margin", 0.2)
    if (
        not isinstance(raw_margin, (int, float))
        or isinstance(raw_margin, bool)
        or not math.isfinite(raw_margin)
        or not 0 <= raw_margin <= 1
    ):
        raise ScorecardError("margin must be a finite number between 0 and 1")
    return float(raw_margin)


def _comparison_object(
    scorecard: dict[str, object],
    comparison_names: tuple[str, ...] = ALL_COMPARISONS,
) -> dict[str, object]:
    comparisons = scorecard.get("comparisons")
    if not isinstance(comparisons, dict):
        raise ScorecardError("comparisons must be a JSON object")
    missing = [name for name in comparison_names if name not in comparisons]
    extra = [name for name in comparisons if name not in comparison_names]
    if missing or extra:
        raise ScorecardError(f"comparisons mismatch; missing={missing}, extra={extra}")
    return comparisons


def _aggregate_counts(scorecard: dict[str, object]) -> dict[str, dict[str, int]]:
    comparisons = _comparison_object(scorecard)
    counts_by_comparison: dict[str, dict[str, int]] = {}
    claim_counts: dict[str, int] = {}
    for name in ALL_COMPARISONS:
        raw_counts = comparisons[name]
        if not isinstance(raw_counts, dict):
            raise ScorecardError(f"comparisons.{name} must be an object")
        counts = {
            key: _score_count(raw_counts.get(key), f"comparisons.{name}.{key}")
            for key in (
                "overlap",
                "different_reason",
                "left_unique",
                "right_unique",
                "ambiguous",
            )
        }
        left_name, right_name = name.split(":", 1)
        paired = counts["overlap"] + counts["different_reason"] + counts["ambiguous"]
        for run_name, claim_count in (
            (left_name, paired + counts["left_unique"]),
            (right_name, paired + counts["right_unique"]),
        ):
            previous = claim_counts.setdefault(run_name, claim_count)
            if previous != claim_count:
                raise ScorecardError(
                    f"{run_name} claim count is inconsistent across comparisons: "
                    f"expected {previous}, found {claim_count} in {name}"
                )
        counts_by_comparison[name] = counts
    return counts_by_comparison


def _printable_identifier(value: object, path: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ScorecardError(f"{path} must be 1..128 printable characters")
    return value


def _dynamic_layout(
    scorecard: dict[str, object],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    runs = scorecard.get("runs")
    if not isinstance(runs, dict):
        raise ScorecardError("runs must be a JSON object")
    raw_order = scorecard.get("run_order")
    if (
        not isinstance(raw_order, list)
        or any(not isinstance(name, str) for name in raw_order)
        or len(raw_order) != len(set(raw_order))
    ):
        raise ScorecardError("run_order must be a unique string list")
    run_names = tuple(raw_order)
    missing = [name for name in runs if name not in run_names]
    extra = [name for name in run_names if name not in runs]
    if missing or extra:
        raise ScorecardError(f"run_order mismatch; missing={missing}, extra={extra}")

    protocol_by_run: dict[str, str] = {}
    seen_repetitions: set[tuple[str, int]] = set()
    group_sizes: dict[str, int] = {}
    repetitions_by_protocol: dict[str, set[int]] = {}
    for run_name in run_names:
        if RUN_LABEL_PATTERN.fullmatch(run_name) is None:
            raise ScorecardError(
                f"run_order label must be printable, colon-free, and at most 128 characters: "
                f"{run_name!r}"
            )
        run = runs[run_name]
        if not isinstance(run, dict):
            raise ScorecardError(f"runs.{run_name} must be an object")
        protocol = _printable_identifier(
            run.get("protocol"), f"runs.{run_name}.protocol"
        )
        repetition = run.get("repetition")
        if (
            not isinstance(repetition, int)
            or isinstance(repetition, bool)
            or repetition <= 0
        ):
            raise ScorecardError(f"runs.{run_name}.repetition must be a positive integer")
        run_key = (protocol, repetition)
        if run_key in seen_repetitions:
            raise ScorecardError(f"duplicate protocol/repetition in runs: {run_key}")
        seen_repetitions.add(run_key)
        protocol_by_run[run_name] = protocol
        group_sizes[protocol] = group_sizes.get(protocol, 0) + 1
        repetitions_by_protocol.setdefault(protocol, set()).add(repetition)

    if len(group_sizes) < 2:
        raise ScorecardError("schema v3 requires at least two protocols")
    if any(size < 2 for size in group_sizes.values()):
        raise ScorecardError("schema v3 requires at least two runs per protocol")
    if len(set(group_sizes.values())) != 1:
        raise ScorecardError("schema v3 requires the same repeat count for every protocol")
    repeat = next(iter(group_sizes.values()))
    expected_repetitions = set(range(1, repeat + 1))
    if any(
        repetitions != expected_repetitions
        for repetitions in repetitions_by_protocol.values()
    ):
        raise ScorecardError("schema v3 repetitions must be continuous from 1 to R")

    within: list[str] = []
    between: list[str] = []
    for left, right in combinations(run_names, 2):
        name = f"{left}:{right}"
        target = (
            within
            if protocol_by_run[left] == protocol_by_run[right]
            else between
        )
        target.append(name)
    return run_names, tuple(within), tuple(between)


def _claim_inventories(
    scorecard: dict[str, object],
    run_names: tuple[str, ...] = RUN_NAMES,
) -> dict[str, set[str]]:
    runs = scorecard.get("runs")
    if not isinstance(runs, dict):
        raise ScorecardError("runs must be a JSON object")
    missing = [name for name in run_names if name not in runs]
    extra = [name for name in runs if name not in run_names]
    if missing or extra:
        raise ScorecardError(f"runs mismatch; missing={missing}, extra={extra}")

    inventories: dict[str, set[str]] = {}
    for run_name in run_names:
        run = runs[run_name]
        if not isinstance(run, dict):
            raise ScorecardError(f"runs.{run_name} must be an object")
        for field in ("archive", "report_sha256"):
            if not isinstance(run.get(field), str) or not run[field]:
                raise ScorecardError(f"runs.{run_name}.{field} must be a non-empty string")
        if SHA256_PATTERN.fullmatch(run["report_sha256"]) is None:
            raise ScorecardError(f"runs.{run_name}.report_sha256 must be lowercase SHA-256")
        claims = run.get("claims")
        if not isinstance(claims, list):
            raise ScorecardError(f"runs.{run_name}.claims must be a list")
        identifiers: set[str] = set()
        for index, claim in enumerate(claims):
            path = f"runs.{run_name}.claims[{index}]"
            if not isinstance(claim, dict):
                raise ScorecardError(f"{path} must be an object")
            identifier = claim.get("id")
            if not isinstance(identifier, str) or CLAIM_ID_PATTERN.fullmatch(identifier) is None:
                raise ScorecardError(f"{path}.id must be A1..An")
            if identifier in identifiers:
                raise ScorecardError(f"runs.{run_name} contains duplicate claim {identifier}")
            identifiers.add(identifier)
            for field in ("position", "claim", "reason"):
                if not isinstance(claim.get(field), str) or not claim[field].strip():
                    raise ScorecardError(f"{path}.{field} must be a non-empty string")
        expected = {f"A{index}" for index in range(1, len(identifiers) + 1)}
        if identifiers != expected:
            raise ScorecardError(
                f"runs.{run_name} claim IDs must be continuous A1..An; found {sorted(identifiers)}"
            )
        inventories[run_name] = identifiers
    return inventories


def _pairing_counts(
    scorecard: dict[str, object],
    run_names: tuple[str, ...] = RUN_NAMES,
    comparison_names: tuple[str, ...] = ALL_COMPARISONS,
    *,
    require_complete: bool = True,
) -> dict[str, dict[str, int]]:
    inventories = _claim_inventories(scorecard, run_names)
    comparisons = _comparison_object(scorecard, comparison_names)
    counts_by_comparison: dict[str, dict[str, int]] = {}
    for name in comparison_names:
        comparison = comparisons[name]
        if not isinstance(comparison, dict):
            raise ScorecardError(f"comparisons.{name} must be an object")
        complete = comparison.get("complete")
        if not isinstance(complete, bool):
            raise ScorecardError(f"comparisons.{name}.complete must be boolean")
        if require_complete and complete is not True:
            raise ScorecardError(f"comparisons.{name}.complete must be true after blind pairing")
        pairs = comparison.get("pairs")
        if not isinstance(pairs, list):
            raise ScorecardError(f"comparisons.{name}.pairs must be a list")
        left_name, right_name = name.split(":", 1)
        used_left: set[str] = set()
        used_right: set[str] = set()
        counts = {
            "overlap": 0,
            "different_reason": 0,
            "left_unique": 0,
            "right_unique": 0,
            "ambiguous": 0,
        }
        for index, pair in enumerate(pairs):
            path = f"comparisons.{name}.pairs[{index}]"
            if not isinstance(pair, dict):
                raise ScorecardError(f"{path} must be an object")
            left = pair.get("left")
            right = pair.get("right")
            classification = pair.get("classification")
            if left not in inventories[left_name]:
                raise ScorecardError(f"{path}.left is not a claim in {left_name}: {left!r}")
            if right not in inventories[right_name]:
                raise ScorecardError(f"{path}.right is not a claim in {right_name}: {right!r}")
            if left in used_left or right in used_right:
                raise ScorecardError(f"{path} violates one-to-one pairing")
            if classification not in PAIR_CLASSIFICATIONS:
                raise ScorecardError(
                    f"{path}.classification must be overlap, different_reason, or ambiguous"
                )
            used_left.add(left)
            used_right.add(right)
            counts[classification] += 1
        counts["left_unique"] = len(inventories[left_name] - used_left)
        counts["right_unique"] = len(inventories[right_name] - used_right)
        counts_by_comparison[name] = counts
    return counts_by_comparison


def validate_pairing_scorecard(scorecard: object) -> None:
    """Validate a traceable scorecard draft without requiring completed pairing."""
    if not isinstance(scorecard, dict):
        raise ScorecardError("scorecard must be a JSON object")
    schema_version = scorecard.get("schema_version")
    if schema_version == 2:
        run_names = RUN_NAMES
        comparison_names = ALL_COMPARISONS
    elif schema_version == 3:
        run_names, within, between = _dynamic_layout(scorecard)
        comparison_names = within + between
    else:
        raise ScorecardError("traceable scorecard schema_version must be 2 or 3")
    _margin(scorecard)
    _pairing_counts(
        scorecard,
        run_names,
        comparison_names,
        require_complete=False,
    )


def _traceable_layout(
    scorecard: dict[str, object],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    schema_version = scorecard.get("schema_version")
    if schema_version == 2:
        return RUN_NAMES, ALL_COMPARISONS
    if schema_version == 3:
        run_names, within, between = _dynamic_layout(scorecard)
        return run_names, within + between
    raise ScorecardError("traceable scorecard schema_version must be 2 or 3")


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _scorecard_evidence_fingerprint(
    scorecard: dict[str, object],
    run_names: tuple[str, ...],
    comparison_names: tuple[str, ...],
) -> str:
    runs = scorecard["runs"]
    evidence = {
        "schema_version": scorecard["schema_version"],
        "margin": _margin(scorecard),
        "run_order": list(run_names),
        "runs": {name: runs[name] for name in run_names},
        "comparisons": list(comparison_names),
    }
    return _canonical_sha256(evidence)


def normalize_blind_seed(raw_seed: object) -> str:
    if (
        not isinstance(raw_seed, str)
        or not raw_seed
        or len(raw_seed) > 128
        or any(ord(character) < 32 or ord(character) == 127 for character in raw_seed)
    ):
        raise ScorecardError("blind seed must be 1..128 printable characters")
    return raw_seed


def create_blind_bundle(
    scorecard: object,
    blind_seed: object,
) -> tuple[dict[str, object], dict[str, object]]:
    """Create a reviewer artifact and a separate identity key."""
    validate_pairing_scorecard(scorecard)
    assert isinstance(scorecard, dict)
    seed = normalize_blind_seed(blind_seed)
    run_names, comparison_names = _traceable_layout(scorecard)
    ranked_runs = sorted(
        run_names,
        key=lambda name: (
            hashlib.sha256(f"blind-v1\0{seed}\0run\0{name}".encode()).hexdigest(),
            name,
        ),
    )
    alias_by_run = {
        name: f"R{index:02d}" for index, name in enumerate(ranked_runs, start=1)
    }
    alias_to_run = {alias: name for name, alias in alias_by_run.items()}
    source_fingerprint = _scorecard_evidence_fingerprint(
        scorecard, run_names, comparison_names
    )
    blind_nonce = hashlib.sha256(
        f"blind-v1\0{seed}\0nonce".encode("utf-8")
    ).hexdigest()
    key_core = {
        "schema_version": 1,
        "source_fingerprint": source_fingerprint,
        "blind_nonce": blind_nonce,
        "alias_to_run": alias_to_run,
    }
    key_id = _canonical_sha256(key_core)
    key = {**key_core, "key_id": key_id}

    source_runs = scorecard["runs"]
    source_comparisons = scorecard["comparisons"]
    blind_runs = {
        alias: {"claims": copy.deepcopy(source_runs[name]["claims"])}
        for alias, name in alias_to_run.items()
    }
    blind_comparisons: dict[str, object] = {}
    ranked_comparisons = sorted(
        comparison_names,
        key=lambda name: (
            hashlib.sha256(
                f"blind-v1\0{seed}\0comparison\0{name}".encode()
            ).hexdigest(),
            name,
        ),
    )
    for name in ranked_comparisons:
        left, right = name.split(":", 1)
        blind_name = f"{alias_by_run[left]}:{alias_by_run[right]}"
        blind_comparisons[blind_name] = copy.deepcopy(source_comparisons[name])
    blind = {
        "schema_version": 1,
        "key_id": key_id,
        "instructions": (
            "Pair claims one-to-one without access to the identity key. Set each "
            "classification to overlap, different_reason, or ambiguous, then set "
            "complete=true. Do not edit runs or claims."
        ),
        "runs": blind_runs,
        "comparisons": blind_comparisons,
    }
    return blind, key


def apply_blind_pairings(
    scorecard: object,
    blind: object,
    key: object,
) -> dict[str, object]:
    """Verify and merge blinded human pairings into a traceable scorecard."""
    validate_pairing_scorecard(scorecard)
    if not isinstance(scorecard, dict):
        raise ScorecardError("scorecard must be a JSON object")
    if not isinstance(blind, dict) or blind.get("schema_version") != 1:
        raise ScorecardError("blind artifact schema_version must be 1")
    if not isinstance(key, dict) or key.get("schema_version") != 1:
        raise ScorecardError("blind key schema_version must be 1")

    run_names, comparison_names = _traceable_layout(scorecard)
    source_fingerprint = _scorecard_evidence_fingerprint(
        scorecard, run_names, comparison_names
    )
    if key.get("source_fingerprint") != source_fingerprint:
        raise ScorecardError("blind key does not match scorecard evidence")

    alias_to_run = key.get("alias_to_run")
    if (
        not isinstance(alias_to_run, dict)
        or any(
            not isinstance(alias, str)
            or BLIND_ALIAS_PATTERN.fullmatch(alias) is None
            or not isinstance(name, str)
            for alias, name in alias_to_run.items()
        )
        or len(alias_to_run) != len(set(alias_to_run.values()))
        or set(alias_to_run.values()) != set(run_names)
    ):
        raise ScorecardError("blind key alias_to_run is invalid")
    key_core = {
        "schema_version": 1,
        "source_fingerprint": source_fingerprint,
        "blind_nonce": key.get("blind_nonce"),
        "alias_to_run": alias_to_run,
    }
    if SHA256_PATTERN.fullmatch(str(key_core["blind_nonce"])) is None:
        raise ScorecardError("blind key nonce is invalid")
    key_id = _canonical_sha256(key_core)
    if key.get("key_id") != key_id or blind.get("key_id") != key_id:
        raise ScorecardError("blind artifact and identity key do not match")

    blind_runs = blind.get("runs")
    if not isinstance(blind_runs, dict) or set(blind_runs) != set(alias_to_run):
        raise ScorecardError("blind artifact runs do not match identity key")
    source_runs = scorecard["runs"]
    for alias, name in alias_to_run.items():
        run = blind_runs[alias]
        if not isinstance(run, dict) or set(run) != {"claims"}:
            raise ScorecardError(f"blind runs.{alias} must contain only claims")
        if run.get("claims") != source_runs[name]["claims"]:
            raise ScorecardError(f"blind runs.{alias}.claims were modified")

    run_to_alias = {name: alias for alias, name in alias_to_run.items()}
    blind_name_by_source: dict[str, str] = {}
    for name in comparison_names:
        left, right = name.split(":", 1)
        blind_name_by_source[name] = f"{run_to_alias[left]}:{run_to_alias[right]}"
    blind_comparisons = blind.get("comparisons")
    expected_blind_names = set(blind_name_by_source.values())
    if (
        not isinstance(blind_comparisons, dict)
        or set(blind_comparisons) != expected_blind_names
    ):
        raise ScorecardError("blind artifact comparisons do not match scorecard")

    merged = copy.deepcopy(scorecard)
    for source_name, blind_name in blind_name_by_source.items():
        comparison = blind_comparisons[blind_name]
        if not isinstance(comparison, dict) or set(comparison) != {"complete", "pairs"}:
            raise ScorecardError(
                f"blind comparisons.{blind_name} must contain complete and pairs"
            )
        merged["comparisons"][source_name] = copy.deepcopy(comparison)
    validate_pairing_scorecard(merged)
    return merged


def _score_counts(
    counts_by_comparison: dict[str, dict[str, int]],
    *,
    margin: float,
    schema_version: int,
    within_comparisons: tuple[str, ...] = WITHIN_COMPARISONS,
    between_comparisons: tuple[str, ...] = BETWEEN_COMPARISONS,
) -> dict[str, object]:
    results: dict[str, dict[str, object]] = {}
    comparison_order = within_comparisons + between_comparisons
    for name in comparison_order:
        counts = counts_by_comparison[name]
        unique = counts["left_unique"] + counts["right_unique"]
        denominator = (
            counts["overlap"]
            + counts["different_reason"]
            + unique
            + counts["ambiguous"]
        )
        lower_numerator = counts["different_reason"] + unique
        upper_numerator = lower_numerator + counts["ambiguous"]
        lower = 0.0 if denominator == 0 else lower_numerator / denominator
        upper = 0.0 if denominator == 0 else upper_numerator / denominator
        paired = counts["overlap"] + counts["different_reason"] + counts["ambiguous"]
        results[name] = {
            **counts,
            "left_claims": paired + counts["left_unique"],
            "right_claims": paired + counts["right_unique"],
            "denominator": denominator,
            "lower_numerator": lower_numerator,
            "upper_numerator": upper_numerator,
            "d_lower": lower,
            "d_upper": upper,
        }

    def average_bound(names: tuple[str, ...], key: str) -> float:
        return sum(float(results[name][key]) for name in names) / len(names)

    w_lower = average_bound(within_comparisons, "d_lower")
    w_upper = average_bound(within_comparisons, "d_upper")
    b_lower = average_bound(between_comparisons, "d_lower")
    b_upper = average_bound(between_comparisons, "d_upper")
    if b_upper <= w_lower:
        verdict = "reject"
        reason = "B is certainly no greater than W"
    elif b_lower >= w_upper + margin:
        verdict = "advance"
        reason = "B is certainly at least W plus the configured margin"
    else:
        verdict = "inconclusive"
        reason = "the uncertainty interval or margin boundary changes the decision"

    return {
        "schema_version": schema_version,
        "margin": margin,
        "comparison_order": list(comparison_order),
        "within_comparisons": list(within_comparisons),
        "between_comparisons": list(between_comparisons),
        "comparisons": results,
        "W": {"lower": w_lower, "upper": w_upper},
        "B": {"lower": b_lower, "upper": b_upper},
        "verdict": verdict,
        "reason": reason,
    }


def score_divergence(scorecard: object) -> dict[str, object]:
    if not isinstance(scorecard, dict):
        raise ScorecardError("scorecard must be a JSON object")
    schema_version = scorecard.get("schema_version")
    if schema_version == 1:
        counts = _aggregate_counts(scorecard)
        within = WITHIN_COMPARISONS
        between = BETWEEN_COMPARISONS
    elif schema_version == 2:
        counts = _pairing_counts(scorecard)
        within = WITHIN_COMPARISONS
        between = BETWEEN_COMPARISONS
    elif schema_version == 3:
        run_names, within, between = _dynamic_layout(scorecard)
        counts = _pairing_counts(scorecard, run_names, within + between)
    else:
        raise ScorecardError("scorecard schema_version must be 1, 2, or 3")
    return _score_counts(
        counts,
        margin=_margin(scorecard),
        schema_version=schema_version,
        within_comparisons=within,
        between_comparisons=between,
    )


def score_markdown(result: dict[str, object]) -> str:
    comparisons = result["comparisons"]
    lines = [
        "# Divergence score",
        "",
        "> Definition: d, W, and B are interval proportions calculated from human classification counts. They are not parametric statistical tests, variance estimates, or Cohen's d.",
        "",
        "| Comparison | numerator | denominator | d interval |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name in result.get("comparison_order", ALL_COMPARISONS):
        item = comparisons[name]
        lower_num = item["lower_numerator"]
        upper_num = item["upper_numerator"]
        numerator = str(lower_num) if lower_num == upper_num else f"{lower_num}–{upper_num}"
        lines.append(
            f"| {name} | {numerator} | {item['denominator']} | "
            f"{item['d_lower']:.3f}–{item['d_upper']:.3f} |"
        )
    lines.extend(
        [
            "",
            f"- W: {result['W']['lower']:.3f}–{result['W']['upper']:.3f}",
            f"- B: {result['B']['lower']:.3f}–{result['B']['upper']:.3f}",
            f"- Margin: {result['margin']:.3f}",
            f"- Verdict: **{result['verdict']}** — {result['reason']}",
            "",
        ]
    )
    return "\n".join(lines)
