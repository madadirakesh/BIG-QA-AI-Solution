"""
report_generator.py
--------------------
Builds a consolidated JSON summary and a self-contained HTML report from:
    - Locust's *_stats.csv       (per-request aggregate stats)
    - Locust's *_stats_history.csv (time series, used for charts)
    - Locust's *_failures.csv    (failure details, if any)
    - ThresholdEvaluator results (pass/fail)

Also opens the generated HTML report in the default web browser.
"""

import csv
import json
import webbrowser
from datetime import datetime
from pathlib import Path


def _read_csv(path):
    p = Path(path)
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build_json_summary(result_dir, test_name, csv_prefix, threshold_result, meta=None):
    result_dir = Path(result_dir)
    stats_rows = _read_csv(f"{csv_prefix}_stats.csv")
    failure_rows = _read_csv(f"{csv_prefix}_failures.csv")

    summary = {
        "test_name": test_name,
        "generated_at": datetime.now().isoformat(),
        "meta": meta or {},
        "requests": stats_rows,
        "failures": failure_rows,
        "thresholds": threshold_result,
    }

    out_path = result_dir / "summary.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2)
    return out_path, summary


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Performance Test Report - {test_name}</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 0; background: #f4f6f8; color: #1c2733; }}
  header {{ background: #1c2733; color: #fff; padding: 24px 32px; }}
  header h1 {{ margin: 0 0 4px 0; font-size: 22px; }}
  header p {{ margin: 0; color: #9fb0c3; font-size: 13px; }}
  .container {{ padding: 24px 32px; }}
  .status-banner {{ padding: 14px 20px; border-radius: 8px; font-weight: 600; margin-bottom: 20px; font-size: 15px; }}
  .status-pass {{ background: #d4f7de; color: #146c34; border: 1px solid #8fe0a8; }}
  .status-fail {{ background: #fbdada; color: #9c1c1c; border: 1px solid #f0a0a0; }}
  .cards {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }}
  .card {{ background: #fff; border-radius: 10px; padding: 16px 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); min-width: 160px; flex: 1; }}
  .card .label {{ font-size: 12px; text-transform: uppercase; color: #6b7a8c; letter-spacing: .04em; }}
  .card .value {{ font-size: 24px; font-weight: 700; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 10px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); margin-bottom: 24px; }}
  th, td {{ padding: 10px 14px; text-align: left; font-size: 13px; border-bottom: 1px solid #eef1f4; }}
  th {{ background: #eef2f6; color: #33455a; font-weight: 600; }}
  tr:last-child td {{ border-bottom: none; }}
  .pass {{ color: #146c34; font-weight: 600; }}
  .fail {{ color: #9c1c1c; font-weight: 600; }}
  h2 {{ font-size: 16px; margin: 28px 0 12px 0; color: #1c2733; }}
  .empty {{ color: #6b7a8c; font-style: italic; padding: 12px; }}
</style>
</head>
<body>
<header>
  <h1>Performance Test Report</h1>
  <p>{test_name} &middot; generated {generated_at}</p>
</header>
<div class="container">
  <div class="status-banner {status_class}">Overall result: {status_text}</div>

  <div class="cards">
    <div class="card"><div class="label">Total Requests</div><div class="value">{total_requests}</div></div>
    <div class="card"><div class="label">Total Failures</div><div class="value">{total_failures}</div></div>
    <div class="card"><div class="label">Error Rate</div><div class="value">{error_rate}%</div></div>
    <div class="card"><div class="label">Avg RPS</div><div class="value">{avg_rps}</div></div>
    <div class="card"><div class="label">Avg Response (ms)</div><div class="value">{avg_response}</div></div>
    <div class="card"><div class="label">P95 (ms)</div><div class="value">{p95_response}</div></div>
  </div>

  <h2>Request Statistics</h2>
  {stats_table}

  <h2>Threshold Checks</h2>
  {threshold_table}

  <h2>Failures</h2>
  {failures_table}
</div>
</body>
</html>
"""


def _build_stats_table(stats_rows):
    if not stats_rows:
        return '<div class="empty">No stats recorded.</div>'
    headers = ["Type", "Name", "Request Count", "Failure Count", "Median Response Time",
               "Average Response Time", "Min Response Time", "Max Response Time",
               "95%", "99%", "Requests/s", "Failures/s"]
    present = [h for h in headers if any(h in r for r in stats_rows)]
    rows_html = "".join(
        "<tr>" + "".join(f"<td>{r.get(h, '')}</td>" for h in present) + "</tr>"
        for r in stats_rows
    )
    head_html = "".join(f"<th>{h}</th>" for h in present)
    return f"<table><tr>{head_html}</tr>{rows_html}</table>"


def _build_threshold_table(threshold_result):
    checks = threshold_result.get("checks", [])
    if not checks:
        return '<div class="empty">No thresholds configured.</div>'
    rows_html = ""
    for c in checks:
        cls = "pass" if c["passed"] else "fail"
        text = "PASS" if c["passed"] else "FAIL"
        rows_html += (
            f"<tr><td>{c['name']}</td><td>{c['metric']}</td>"
            f"<td>{c['limit']}</td><td>{c['actual']}</td>"
            f"<td class='{cls}'>{text}</td></tr>"
        )
    return (
        "<table><tr><th>Request</th><th>Metric</th><th>Limit</th>"
        f"<th>Actual</th><th>Result</th></tr>{rows_html}</table>"
    )


def _build_failures_table(failure_rows):
    if not failure_rows:
        return '<div class="empty">No failures recorded.</div>'
    headers = list(failure_rows[0].keys())
    rows_html = "".join(
        "<tr>" + "".join(f"<td>{r.get(h, '')}</td>" for h in headers) + "</tr>"
        for r in failure_rows
    )
    head_html = "".join(f"<th>{h}</th>" for h in headers)
    return f"<table><tr>{head_html}</tr>{rows_html}</table>"


def build_html_report(result_dir, test_name, csv_prefix, threshold_result):
    result_dir = Path(result_dir)
    stats_rows = _read_csv(f"{csv_prefix}_stats.csv")
    failure_rows = _read_csv(f"{csv_prefix}_failures.csv")

    agg = next((r for r in stats_rows if r["Name"].strip().lower() == "aggregated"), None)
    total_requests = agg.get("Request Count", "0") if agg else "0"
    total_failures = agg.get("Failure Count", "0") if agg else "0"
    try:
        error_rate = round(float(total_failures) / float(total_requests) * 100, 2) if float(total_requests) else 0.0
    except (ValueError, ZeroDivisionError):
        error_rate = 0.0

    html = HTML_TEMPLATE.format(
        test_name=test_name,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        status_class="status-pass" if threshold_result.get("passed", True) else "status-fail",
        status_text="PASSED" if threshold_result.get("passed", True) else "FAILED",
        total_requests=total_requests,
        total_failures=total_failures,
        error_rate=error_rate,
        avg_rps=agg.get("Requests/s", "0") if agg else "0",
        avg_response=agg.get("Average Response Time", "0") if agg else "0",
        p95_response=agg.get("95%", "0") if agg else "0",
        stats_table=_build_stats_table(stats_rows),
        threshold_table=_build_threshold_table(threshold_result),
        failures_table=_build_failures_table(failure_rows),
    )

    out_path = result_dir / "report.html"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    return out_path


def open_report(html_path):
    """Open the HTML report in the default browser."""
    try:
        webbrowser.open(f"file://{Path(html_path).resolve()}")
    except Exception as e:
        print(f"[report_generator] Could not auto-open report: {e}")
