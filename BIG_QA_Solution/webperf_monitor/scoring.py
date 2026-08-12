"""
Reimplementation of Lighthouse's log-normal metric scoring curves.

Lighthouse scores each metric with a log-normal CDF calibrated so that:
  - the value at the metric's "p10" (10th percentile / good) control point
    scores 0.9
  - the value at the metric's "median" control point scores 0.5

Control point constants below are Lighthouse's published v10 desktop/mobile
scoring curve constants (in ms, except CLS which is unitless).
"""

import math

# erfinv(0.8) precomputed - this is the constant Lighthouse's scoring curve
# resolves to for the fixed 0.9-at-p10 calibration point.
_ERFINV_0_8 = 0.9061938024368232
_SQRT2 = math.sqrt(2)

# (p10, median) control points, from Lighthouse's metric scoring config.
CONTROL_POINTS = {
    "fcp": (1800, 3000),
    "si": (3387, 5800),
    "lcp": (2500, 4000),
    "tbt": (200, 600),
    "cls": (0.1, 0.25),
    # INP (Interaction to Next Paint) is scored and displayed but, matching
    # Lighthouse, it is NOT part of the weighted performance score (it's a
    # field metric). p10/median from Lighthouse's INP thresholds.
    "inp": (200, 500),
}

# Lighthouse v10 performance-score weights: FCP 10, SI 10, LCP 25, TBT 30,
# CLS 25. Speed Index is now measured (from the filmstrip, when Pillow is
# available); when it can't be measured it is dropped and the remaining
# weights are renormalized by compute_performance_score().
WEIGHTS = {
    "fcp": 0.10,
    "si": 0.10,
    "lcp": 0.25,
    "tbt": 0.30,
    "cls": 0.25,
}


def log_normal_score(value: float, median: float, p10: float) -> float:
    """Return a 0.0-1.0 score for `value` given Lighthouse-style control points."""
    if value is None:
        return None  # type: ignore[return-value]
    if value <= 0:
        return 1.0

    location = math.log(median)
    shape = (math.log(p10) - location) / (_SQRT2 * _ERFINV_0_8)
    standardized = (math.log(value) - location) / shape
    score = 0.5 * (1 + math.erf(standardized / _SQRT2))
    return max(0.0, min(1.0, score))


def score_metric(name: str, value):
    if value is None or name not in CONTROL_POINTS:
        return None
    p10, median = CONTROL_POINTS[name]
    return round(log_normal_score(value, median, p10), 4)


def rating_for_score(score) -> str:
    if score is None:
        return "unknown"
    if score >= 0.9:
        return "good"
    if score >= 0.5:
        return "needs-improvement"
    return "poor"


def compute_performance_score(metric_scores: dict) -> float:
    """Weighted average of available metric scores (0-100 scale)."""
    total_weight = 0.0
    total = 0.0
    for name, weight in WEIGHTS.items():
        score = metric_scores.get(name)
        if score is None:
            continue
        total += score * weight
        total_weight += weight

    if total_weight == 0:
        return None  # type: ignore[return-value]
    return round((total / total_weight) * 100)
