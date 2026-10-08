"""
performance_insights.py
-----------------------
Turns the rows kept for performance runs into the figures the Performance
Dashboard draws.

Two tables feed this module, both written at the end of every run:

* **PerformanceRunStats** - the totals of one run (`_stats.csv`): one row per
  endpoint plus the trailing "Aggregated" row.
* **PerformanceRunHistory** - the samples of one run (`_stats_history.csv`):
  one row per interval, covering all endpoints together. This is the only
  source of *within-run* time, so a run recorded before history was captured
  simply has no timeline and the dashboard says so rather than inventing one.

Everything here is a pure function over those rows so the numbers can be
tested without a database, and so the route stays a route.

A note on percentiles under an endpoint-type filter: percentiles cannot be
added up or averaged across endpoints, so when the view is narrowed to GET or
POST the cards fall back to the *worst* endpoint's percentile and say so
(`percentile_basis`). Only the unfiltered view can use Locust's own aggregated
percentiles, which are computed from the raw samples.
"""

import math

AGGREGATED_NAME = "Aggregated"

# Percent change in the 99th percentile between the first and last run in view
# that is treated as a real move rather than run-to-run noise.
DEGRADATION_TOLERANCE_PCT = 10.0
# Throughput spread (standard deviation over the mean) above which identical
# runs are not actually delivering identical load.
THROUGHPUT_COV_TOLERANCE_PCT = 15.0
# |r| at or above which content size and response time are called related.
PAYLOAD_CORRELATION_TOLERANCE = 0.6


# ---------------------------------------------------------------------------
# Small numeric helpers. Every measurement can legitimately be NULL (Locust
# writes "N/A" for a percentile it has no samples for), so nothing here may
# assume a value is present.
# ---------------------------------------------------------------------------

def _num(value):
    """A row cell as a float, or None when it is missing or not a number."""
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _total(rows, field):
    """Sum a field over rows, ignoring the rows that have no value for it."""
    values = [_num(row.get(field)) for row in rows]
    present = [value for value in values if value is not None]
    return sum(present) if present else None


def _largest(rows, field):
    values = [_num(row.get(field)) for row in rows]
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _smallest(rows, field):
    values = [_num(row.get(field)) for row in rows]
    present = [value for value in values if value is not None]
    return min(present) if present else None


def _request_weighted(rows, field):
    """
    Average a per-request field over endpoints, weighted by request count.

    A plain mean would let an endpoint hit twice count as much as one hit ten
    thousand times.
    """
    weighted, weight = 0.0, 0.0
    for row in rows:
        value = _num(row.get(field))
        count = _num(row.get("request_count")) or 0
        if value is None or count <= 0:
            continue
        weighted += value * count
        weight += count
    return weighted / weight if weight else None


def _rate(part, whole):
    """`part` as a percentage of `whole`; None when there is nothing to divide."""
    if part is None or not whole:
        return None
    return (part / whole) * 100.0


def _change_pct(first, last):
    if first in (None, 0) or last is None:
        return None
    return ((last - first) / first) * 100.0


def _correlation(pairs):
    """Pearson r for (x, y) pairs; None when it is not defined."""
    points = [(x, y) for x, y in pairs if x is not None and y is not None]
    if len(points) < 3:
        return None
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    mean_x, mean_y = sum(xs) / len(xs), sum(ys) / len(ys)
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    denominator = math.sqrt(sum(v * v for v in dx)) * math.sqrt(sum(v * v for v in dy))
    if not denominator:
        return None
    return sum(a * b for a, b in zip(dx, dy)) / denominator


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def group_runs(stat_rows):
    """
    Group flat PerformanceRunStats rows into runs, oldest first.

    A run is identified by (script_file, run_id): `run_id` counts per script,
    so two scripts both have a run 1.
    """
    runs = {}
    order = []
    for row in stat_rows:
        key = (row.get("script_file") or "", row.get("run_id"))
        if key not in runs:
            runs[key] = {
                "run_id": row.get("run_id"),
                "script_file": row.get("script_file") or "",
                "run_at": row.get("run_at") or "",
                "concurrent_users": row.get("concurrent_users"),
                "spawn_rate": row.get("spawn_rate"),
                "duration": row.get("duration"),
                "aggregated": None,
                "endpoints": [],
            }
            order.append(key)
        run = runs[key]
        if (row.get("name") or "").strip() == AGGREGATED_NAME:
            run["aggregated"] = row
        else:
            run["endpoints"].append(row)

    ordered = [runs[key] for key in order]
    ordered.sort(key=lambda run: (run.get("run_at") or "", run.get("run_id") or 0))
    return ordered


def run_label(run):
    """
    A run's dropdown label: `Run 3 · 2026-09-14 10:22`.

    The script is not repeated here - the page already has a test-case filter,
    and the name is long enough to push the timestamp out of a select.
    """
    stamp = (run.get("run_at") or "").strip()
    parts = [f"Run {run.get('run_id')}", stamp[:16]]
    return " · ".join(part for part in parts if part)


def run_identity(run):
    """The fields that name a run to the client and back again."""
    return {
        "run_id": run.get("run_id"),
        "script_file": run.get("script_file") or "",
        "run_at": run.get("run_at") or "",
        "concurrent_users": run.get("concurrent_users"),
        "spawn_rate": run.get("spawn_rate"),
        "duration": run.get("duration"),
        "label": run_label(run),
    }


def find_run(runs, script_file, run_id):
    """
    The run the caller asked for, else the most recent one.

    Falling back keeps the dashboard useful when a stale run is requested (a
    bookmarked filter, a switched project) instead of rendering nothing.
    """
    if run_id is not None:
        for run in runs:
            same_script = not script_file or (run.get("script_file") or "") == script_file
            if run.get("run_id") == run_id and same_script:
                return run
    return runs[-1] if runs else None


def request_methods(stat_rows):
    """The distinct endpoint types (GET, POST, ...) present in the rows."""
    methods = set()
    for row in stat_rows:
        if (row.get("name") or "").strip() == AGGREGATED_NAME:
            continue
        method = (row.get("request_type") or "").strip().upper()
        if method:
            methods.add(method)
    return sorted(methods)


def _scoped_endpoints(run, method):
    """The run's endpoint rows, narrowed to one request type when asked."""
    rows = run.get("endpoints") or []
    if not method or method == "all":
        return rows
    wanted = method.strip().upper()
    return [row for row in rows if (row.get("request_type") or "").strip().upper() == wanted]


# The percentile columns a run carries, in the order the endpoint table shows
# them. `pct_50` duplicates the median column by design - it is what the CSV
# has, and the table mirrors the CSV.
PERCENTILE_COLUMNS = (
    ("pct_50", "50%"), ("pct_66", "66%"), ("pct_75", "75%"), ("pct_80", "80%"),
    ("pct_90", "90%"), ("pct_95", "95%"), ("pct_98", "98%"), ("pct_99", "99%"),
    ("pct_99_9", "99.9%"), ("pct_99_99", "99.99%"), ("pct_100", "100%"),
)


def run_metrics(run, method="all"):
    """
    One run's headline numbers under the current endpoint-type scope.

    Unfiltered, these come straight from Locust's "Aggregated" row. Filtered,
    they are rebuilt from the matching endpoints: counts add up, response times
    are weighted by request count, and percentiles report the worst endpoint
    (percentiles of different endpoints cannot be merged after the fact).
    """
    unfiltered = not method or method == "all"
    aggregated = run.get("aggregated")
    endpoints = _scoped_endpoints(run, method)

    if unfiltered and aggregated:
        source = [aggregated]
        requests = _num(aggregated.get("request_count"))
        failures = _num(aggregated.get("failure_count"))
        metrics = {
            "requests": requests,
            "failures": failures,
            "median": _num(aggregated.get("median_response_time")),
            "average": _num(aggregated.get("average_response_time")),
            "min": _num(aggregated.get("min_response_time")),
            "max": _num(aggregated.get("max_response_time")),
            "content_size": _num(aggregated.get("average_content_size")),
            "requests_per_sec": _num(aggregated.get("requests_per_sec")),
            "failures_per_sec": _num(aggregated.get("failures_per_sec")),
            "percentile_basis": "aggregated",
        }
        for column, _ in PERCENTILE_COLUMNS:
            metrics[column] = _num(aggregated.get(column))
    else:
        source = endpoints
        requests = _total(endpoints, "request_count")
        failures = _total(endpoints, "failure_count")
        metrics = {
            "requests": requests,
            "failures": failures,
            "median": _request_weighted(endpoints, "median_response_time"),
            "average": _request_weighted(endpoints, "average_response_time"),
            "min": _smallest(endpoints, "min_response_time"),
            "max": _largest(endpoints, "max_response_time"),
            "content_size": _request_weighted(endpoints, "average_content_size"),
            "requests_per_sec": _total(endpoints, "requests_per_sec"),
            "failures_per_sec": _total(endpoints, "failures_per_sec"),
            "percentile_basis": "worst-endpoint",
        }
        for column, _ in PERCENTILE_COLUMNS:
            metrics[column] = _largest(endpoints, column)

    metrics["endpoint_count"] = len(endpoints)
    metrics["failure_rate"] = _rate(failures, requests)
    metrics["success_rate"] = None if metrics["failure_rate"] is None else 100.0 - metrics["failure_rate"]
    metrics["source_rows"] = len(source)
    return metrics


def endpoint_table(run, method="all"):
    """
    The selected run's per-endpoint grid, busiest endpoint first.

    Mirrors the columns of `_stats.csv` and adds the error rate, which is what
    a reader actually compares endpoints on.
    """
    rows = []
    for row in _scoped_endpoints(run, method):
        requests = _num(row.get("request_count"))
        failures = _num(row.get("failure_count"))
        entry = {
            "request_type": (row.get("request_type") or "").strip(),
            "name": row.get("name") or "",
            "requests": requests,
            "failures": failures,
            "error_rate": _rate(failures, requests),
            "median": _num(row.get("median_response_time")),
            "average": _num(row.get("average_response_time")),
            "min": _num(row.get("min_response_time")),
            "max": _num(row.get("max_response_time")),
            "content_size": _num(row.get("average_content_size")),
            "requests_per_sec": _num(row.get("requests_per_sec")),
            "failures_per_sec": _num(row.get("failures_per_sec")),
        }
        for column, _ in PERCENTILE_COLUMNS:
            entry[column] = _num(row.get(column))
        rows.append(entry)
    rows.sort(key=lambda entry: entry["requests"] or 0, reverse=True)
    return rows


# ---------------------------------------------------------------------------
# Within one run: the timeline
# ---------------------------------------------------------------------------

def timeline_series(history_rows):
    """
    One run's samples as a timeline, oldest first.

    `elapsed` is seconds since the first sample, so the x-axis reads as time
    into the run rather than as a wall clock - runs of the same script line up
    that way.

    Only the *leading* empty samples are dropped - the ones Locust writes
    before it has measured anything. An empty sample later in the run is a
    measurement: it is the application serving nothing, and the chart has to
    show that gap rather than closing it up and drawing a line straight
    through the outage.
    """
    samples = []
    for row in history_rows:
        stamp = _num(row.get("timestamp"))
        if stamp is None:
            continue
        samples.append({
            "timestamp": stamp,
            "users": _num(row.get("user_count")),
            "requests_per_sec": _num(row.get("requests_per_sec")),
            "failures_per_sec": _num(row.get("failures_per_sec")),
            "median": _num(row.get("pct_50")),
            "pct_90": _num(row.get("pct_90")),
            "pct_95": _num(row.get("pct_95")),
            "pct_99": _num(row.get("pct_99")),
            "average": _num(row.get("total_average_response_time")),
            "requests": _num(row.get("total_request_count")),
            "failures": _num(row.get("total_failure_count")),
            "content_size": _num(row.get("total_average_content_size")),
        })

    samples.sort(key=lambda sample: sample["timestamp"])
    first_measured = next((index for index, sample in enumerate(samples)
                           if sample["median"] is not None or sample["requests_per_sec"]), None)
    if first_measured is None:
        return []
    samples = samples[first_measured:]

    start = samples[0]["timestamp"]
    for sample in samples:
        sample["elapsed"] = round(sample["timestamp"] - start, 1)
    return samples


def peak_throughput(timeline, metrics, method="all"):
    """
    The highest requests/s the run reached, and where that figure came from.

    The timeline holds the real peak, but it only ever covers the run as a
    whole - Locust samples every endpoint together. So under an endpoint-type
    filter the timeline would answer a different question than the rest of the
    panel, and the run's rate for the endpoints in scope is used instead. The
    basis is returned with the number so the card can say which it is showing.
    """
    unfiltered = not method or method == "all"
    rates = [sample["requests_per_sec"] for sample in timeline
             if sample.get("requests_per_sec") is not None]
    if unfiltered and rates:
        return max(rates), "timeline"
    return metrics.get("requests_per_sec"), "run-average" if unfiltered else "scope-average"


# ---------------------------------------------------------------------------
# Across runs: trends and inferences
# ---------------------------------------------------------------------------

def trend_series(runs, method="all"):
    """One entry per run, oldest first - the basis of every cross-run chart."""
    series = []
    for run in runs:
        metrics = run_metrics(run, method)
        series.append({
            "run_id": run.get("run_id"),
            "script_file": run.get("script_file") or "",
            "run_at": run.get("run_at") or "",
            "label": run_label(run),
            "short_label": f"Run {run.get('run_id')}",
            "concurrent_users": run.get("concurrent_users"),
            "requests": metrics["requests"],
            "failures": metrics["failures"],
            "failure_rate": metrics["failure_rate"],
            "average": metrics["average"],
            "median": metrics["median"],
            "pct_95": metrics["pct_95"],
            "pct_99": metrics["pct_99"],
            "max": metrics["max"],
            "requests_per_sec": metrics["requests_per_sec"],
            "content_size": metrics["content_size"],
        })
    return series


def endpoint_variance(runs, method="all"):
    """
    Each endpoint's error rate and response time across the runs in view.

    An endpoint that fails in one run out of six is intermittent; one that
    fails a little in every run is consistently unhealthy. The two need
    different fixes, and only a per-run series tells them apart.
    """
    tracked = {}
    order = []
    for run in runs:
        label = f"Run {run.get('run_id')}"
        for row in _scoped_endpoints(run, method):
            key = ((row.get("request_type") or "").strip().upper(), row.get("name") or "")
            if key not in tracked:
                tracked[key] = {
                    "request_type": key[0],
                    "name": key[1],
                    "points": [],
                }
                order.append(key)
            requests = _num(row.get("request_count"))
            failures = _num(row.get("failure_count"))
            tracked[key]["points"].append({
                "label": label,
                "run_at": run.get("run_at") or "",
                "error_rate": _rate(failures, requests),
                "average": _num(row.get("average_response_time")),
                "requests": requests,
                "failures": failures,
            })

    entries = []
    for key in order:
        entry = tracked[key]
        rates = [point["error_rate"] for point in entry["points"] if point["error_rate"] is not None]
        failing = [rate for rate in rates if rate > 0]
        entry["runs_seen"] = len(entry["points"])
        entry["max_error_rate"] = max(rates) if rates else None
        entry["mean_error_rate"] = sum(rates) / len(rates) if rates else None
        entry["failing_runs"] = len(failing)
        entry["requests"] = sum(point["requests"] or 0 for point in entry["points"])
        # "Intermittent" is the useful distinction: it failed somewhere and was
        # clean somewhere else, so the endpoint itself is not simply broken.
        if not failing:
            entry["pattern"] = "clean"
        elif len(failing) == len(rates):
            entry["pattern"] = "persistent"
        else:
            entry["pattern"] = "intermittent"
        entries.append(entry)

    entries.sort(key=lambda entry: (entry["max_error_rate"] or 0, entry["requests"]), reverse=True)
    return entries


def _load_spread(trend):
    """
    The range of concurrent users across the runs in view.

    A script check runs one user for 30 seconds and a load run may drive
    hundreds, and both are recorded. Comparing their response times as a trend
    would read a change in the load as a change in the application, so any
    cross-run verdict has to declare when the load was not held constant.
    """
    loads = sorted({int(entry["concurrent_users"]) for entry in trend
                    if entry.get("concurrent_users")})
    if len(loads) < 2:
        return {"load_varies": False}
    return {"load_varies": True, "load_min": loads[0], "load_max": loads[-1]}


def _standard_deviation(values):
    if len(values) < 2:
        return None
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(variance)


def inferences(trend, variance):
    """
    The plain-language read on a set of runs.

    Each verdict is a stated threshold, not a feeling: percent change for
    degradation, spread over mean for throughput consistency, and Pearson r for
    the payload/response-time link. Anything the data cannot answer yet
    (one run, no content sizes) reports `status: "insufficient"` so the panel
    says why instead of showing a confident zero.
    """
    def unavailable(reason):
        return {"status": "insufficient", "detail": reason}

    result = {}
    comparable = len(trend) >= 2
    load = _load_spread(trend)

    # 1. Degradation trending - is the same test getting slower over time?
    if not comparable:
        result["degradation"] = unavailable(
            "At least two runs are needed before a trend can be read.")
    else:
        first, last = trend[0], trend[-1]
        p99_change = _change_pct(first.get("pct_99"), last.get("pct_99"))
        avg_change = _change_pct(first.get("average"), last.get("average"))
        reference = p99_change if p99_change is not None else avg_change
        if reference is None:
            result["degradation"] = unavailable(
                "These runs carry no comparable response-time figures.")
        else:
            if reference > DEGRADATION_TOLERANCE_PCT:
                status, verdict = "bad", "Slowing down"
            elif reference < -DEGRADATION_TOLERANCE_PCT:
                status, verdict = "good", "Speeding up"
            else:
                status, verdict = "steady", "Holding steady"
            result["degradation"] = {
                "status": status,
                "verdict": verdict,
                "p99_change_pct": p99_change,
                "avg_change_pct": avg_change,
                "first_label": first.get("short_label"),
                "last_label": last.get("short_label"),
                "tolerance_pct": DEGRADATION_TOLERANCE_PCT,
                **load,
            }

    # 2. Throughput consistency - is the infrastructure delivering the same
    #    load every time the same test runs?
    rates = [entry["requests_per_sec"] for entry in trend
             if entry.get("requests_per_sec") is not None]
    mean_rate = sum(rates) / len(rates) if rates else 0
    if len(rates) < 2:
        result["throughput"] = unavailable(
            "At least two runs with a recorded request rate are needed.")
    elif not mean_rate:
        # Every run reports zero throughput, so there is no consistency to
        # judge - calling that "varies between runs" would be nonsense.
        result["throughput"] = unavailable(
            "These runs recorded no throughput at all, so there is no rate to compare.")
    else:
        spread = _standard_deviation(rates)
        cov = (spread / mean_rate * 100.0) if spread is not None else None
        result["throughput"] = {
            "status": "steady" if (cov is not None and cov <= THROUGHPUT_COV_TOLERANCE_PCT) else "bad",
            "verdict": ("Consistent" if (cov is not None and cov <= THROUGHPUT_COV_TOLERANCE_PCT)
                        else "Varies between runs"),
            "mean_rps": mean_rate,
            "min_rps": min(rates),
            "max_rps": max(rates),
            "cov_pct": cov,
            "tolerance_pct": THROUGHPUT_COV_TOLERANCE_PCT,
            **load,
        }

    # 3. Payload impact - do heavier responses come back slower?
    pairs = [(entry.get("content_size"), entry.get("average")) for entry in trend]
    usable = [pair for pair in pairs if pair[0] is not None and pair[1] is not None]
    correlation = _correlation(pairs)
    if len(usable) < 3:
        result["payload"] = unavailable(
            "Three or more runs with both a content size and a response time are needed; "
            f"{len(usable)} of these runs have both.")
    elif correlation is None:
        # Enough runs, but one of the two measures never moved - so it cannot
        # be what explains the other.
        flat = "Average content size" if len({pair[0] for pair in usable}) == 1 else "Average response time"
        result["payload"] = unavailable(
            f"{flat} is identical in every run in range, so it cannot explain the other.")
    else:
        strong = abs(correlation) >= PAYLOAD_CORRELATION_TOLERANCE
        if strong and correlation > 0:
            status, verdict = "bad", "Heavier responses run slower"
        elif strong:
            status, verdict = "steady", "Heavier responses run faster"
        else:
            status, verdict = "good", "Size is not driving response time"
        result["payload"] = {
            "status": status,
            "verdict": verdict,
            "correlation": correlation,
            "tolerance": PAYLOAD_CORRELATION_TOLERANCE,
        }

    # 4. Endpoint reliability variance - which route is actually unreliable?
    flagged = [entry for entry in variance if (entry.get("max_error_rate") or 0) > 0]
    if not variance:
        result["reliability"] = unavailable("No endpoint rows were recorded for these runs.")
    elif not flagged:
        result["reliability"] = {
            "status": "good",
            "verdict": "No endpoint failed",
            "endpoint_count": len(variance),
            "intermittent": 0,
            "persistent": 0,
        }
    else:
        worst = flagged[0]
        intermittent = [entry for entry in flagged if entry["pattern"] == "intermittent"]
        persistent = [entry for entry in flagged if entry["pattern"] == "persistent"]
        result["reliability"] = {
            "status": "bad",
            "verdict": ("Intermittent failures" if intermittent else "Consistent failures"),
            "worst_endpoint": f"{worst['request_type']} {worst['name']}".strip(),
            "worst_error_rate": worst.get("max_error_rate"),
            "endpoint_count": len(variance),
            "intermittent": len(intermittent),
            "persistent": len(persistent),
        }

    return result
