"""Deterministic W/B divergence scoring from human one-to-one pairings."""

from __future__ import annotations

import math


WITHIN_COMPARISONS = ("I1:I2", "C1:C2")
BETWEEN_COMPARISONS = ("I1:C1", "I1:C2", "I2:C1", "I2:C2")
ALL_COMPARISONS = WITHIN_COMPARISONS + BETWEEN_COMPARISONS


class ScorecardError(ValueError):
    """Raised when a divergence scorecard cannot be reproduced safely."""


def scorecard_template() -> dict[str, object]:
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


def _score_count(value: object, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ScorecardError(f"{path} must be a non-negative integer")
    return value


def score_divergence(scorecard: object) -> dict[str, object]:
    if not isinstance(scorecard, dict):
        raise ScorecardError("scorecard must be a JSON object")
    if scorecard.get("schema_version") != 1:
        raise ScorecardError("scorecard schema_version must be 1")
    raw_margin = scorecard.get("margin", 0.2)
    if (
        not isinstance(raw_margin, (int, float))
        or isinstance(raw_margin, bool)
        or not math.isfinite(raw_margin)
        or not 0 <= raw_margin <= 1
    ):
        raise ScorecardError("margin must be a finite number between 0 and 1")
    margin = float(raw_margin)

    comparisons = scorecard.get("comparisons")
    if not isinstance(comparisons, dict):
        raise ScorecardError("comparisons must be a JSON object")
    missing = [name for name in ALL_COMPARISONS if name not in comparisons]
    extra = [name for name in comparisons if name not in ALL_COMPARISONS]
    if missing or extra:
        raise ScorecardError(f"comparisons mismatch; missing={missing}, extra={extra}")

    results: dict[str, dict[str, object]] = {}
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
        unique = counts["left_unique"] + counts["right_unique"]
        denominator = (
            counts["overlap"]
            + counts["different_reason"]
            + unique
            + counts["ambiguous"]
        )
        lower_numerator = counts["different_reason"] + unique
        upper_numerator = lower_numerator + counts["ambiguous"]
        if denominator == 0:
            lower = upper = 0.0
        else:
            lower = lower_numerator / denominator
            upper = upper_numerator / denominator
        left_name, right_name = name.split(":", 1)
        paired = counts["overlap"] + counts["different_reason"] + counts["ambiguous"]
        left_claims = paired + counts["left_unique"]
        right_claims = paired + counts["right_unique"]
        for run_name, claim_count in (
            (left_name, left_claims),
            (right_name, right_claims),
        ):
            previous = claim_counts.setdefault(run_name, claim_count)
            if previous != claim_count:
                raise ScorecardError(
                    f"{run_name} claim count is inconsistent across comparisons: "
                    f"expected {previous}, found {claim_count} in {name}"
                )
        results[name] = {
            **counts,
            "left_claims": left_claims,
            "right_claims": right_claims,
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
        "schema_version": 1,
        "margin": margin,
        "comparisons": results,
        "W": {"lower": w_lower, "upper": w_upper},
        "B": {"lower": b_lower, "upper": b_upper},
        "verdict": verdict,
        "reason": reason,
    }


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
