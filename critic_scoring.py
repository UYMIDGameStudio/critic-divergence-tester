"""Deterministic W/B divergence scoring from human one-to-one pairings."""

from __future__ import annotations

import math
import re


WITHIN_COMPARISONS = ("I1:I2", "C1:C2")
BETWEEN_COMPARISONS = ("I1:C1", "I1:C2", "I2:C1", "I2:C2")
ALL_COMPARISONS = WITHIN_COMPARISONS + BETWEEN_COMPARISONS
RUN_NAMES = ("I1", "I2", "C1", "C2")
CLAIM_ID_PATTERN = re.compile(r"A[1-9][0-9]*")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
PAIR_CLASSIFICATIONS = {"overlap", "different_reason", "ambiguous"}


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


def _comparison_object(scorecard: dict[str, object]) -> dict[str, object]:
    comparisons = scorecard.get("comparisons")
    if not isinstance(comparisons, dict):
        raise ScorecardError("comparisons must be a JSON object")
    missing = [name for name in ALL_COMPARISONS if name not in comparisons]
    extra = [name for name in comparisons if name not in ALL_COMPARISONS]
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


def _claim_inventories(
    scorecard: dict[str, object],
) -> dict[str, set[str]]:
    runs = scorecard.get("runs")
    if not isinstance(runs, dict):
        raise ScorecardError("runs must be a JSON object")
    missing = [name for name in RUN_NAMES if name not in runs]
    extra = [name for name in runs if name not in RUN_NAMES]
    if missing or extra:
        raise ScorecardError(f"runs mismatch; missing={missing}, extra={extra}")

    inventories: dict[str, set[str]] = {}
    for run_name in RUN_NAMES:
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


def _pairing_counts(scorecard: dict[str, object]) -> dict[str, dict[str, int]]:
    inventories = _claim_inventories(scorecard)
    comparisons = _comparison_object(scorecard)
    counts_by_comparison: dict[str, dict[str, int]] = {}
    for name in ALL_COMPARISONS:
        comparison = comparisons[name]
        if not isinstance(comparison, dict):
            raise ScorecardError(f"comparisons.{name} must be an object")
        if comparison.get("complete") is not True:
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


def _score_counts(
    counts_by_comparison: dict[str, dict[str, int]],
    *,
    margin: float,
    schema_version: int,
) -> dict[str, object]:
    results: dict[str, dict[str, object]] = {}
    for name in ALL_COMPARISONS:
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

    w_lower = average_bound(WITHIN_COMPARISONS, "d_lower")
    w_upper = average_bound(WITHIN_COMPARISONS, "d_upper")
    b_lower = average_bound(BETWEEN_COMPARISONS, "d_lower")
    b_upper = average_bound(BETWEEN_COMPARISONS, "d_upper")
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
    elif schema_version == 2:
        counts = _pairing_counts(scorecard)
    else:
        raise ScorecardError("scorecard schema_version must be 1 or 2")
    return _score_counts(
        counts,
        margin=_margin(scorecard),
        schema_version=schema_version,
    )


def score_markdown(result: dict[str, object]) -> str:
    comparisons = result["comparisons"]
    lines = [
        "# Divergence score",
        "",
        "| Comparison | numerator | denominator | d interval |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name in ALL_COMPARISONS:
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
