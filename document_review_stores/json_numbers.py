"""JSON decoder callbacks independent from workflow and HTTP dependencies."""
from __future__ import annotations

import math


def finite_json_number(token: str) -> float:
    """Reject nonstandard constants and exponent overflow during decoding."""
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("JSON 数值必须是可表示的有限数，不能使用 NaN、Infinity 或溢出指数")
    return value
