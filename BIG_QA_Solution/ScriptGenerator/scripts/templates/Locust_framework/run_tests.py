#!/usr/bin/env python3
"""
run_tests.py
------------
Main entry point for the performance test automation framework.

Usage:
    python run_tests.py --config config/test_configs/load_test.yaml
    python run_tests.py --config config/test_configs/stress_test.yaml --no-open

Each run:
  1. Creates results/run_<test_run_name>_<YYYYMMDD_HHMMSS>/
  2. Executes every test listed in the config (in parallel if configured,
     each optionally in distributed master/worker mode)
  3. Evaluates pass/fail thresholds per test
  4. Writes a JSON summary + HTML report per test, plus a combined
     index.html for the whole run
  5. Opens the combined HTML report in the default browser
  6. Exits with code 1 if any test failed its thresholds (useful for CI)
"""

import argparse
import sys
import yaml
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.parallel_runner import run_tests_in_parallel, _run_single_test
from core.thresholds import ThresholdEvaluator
from core.report_generator import build_json_summary, build_html_report, open_report

ROOT = Path(__file__).resolve().parent
RESULTS_ROOT = ROOT / "results"

INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Run Summary - {run_name}</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, Arial, sans-serif; margin: 0; background: #f4f6f8; color: #1c2733; }}
  header {{ background: #1c2733; color: #fff; padding: 24px 32px; }}
  header h1 {{ margin: 0 0 4px 0; font-size: 22px; }}
  header p {{ margin: 0; color: #9fb0c3; font-size: 13px; }}
  .container {{ padding: 24px 32px; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 10px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
  th, td {{ padding: 12px 16px; text-align: left; font-size: 14px; border-bottom: 1px solid #eef1f4; }}
  th {{ background: #eef2f6; color: #33455a; }}
  a {{ color: #2451c9; text-decoration: none; font-weight: 600; }}
  .pass {{ color: #146c34; font-weight: 700; }}
  .fail {{ color: #9c1c1c; font-weight: 700; }}
</style>
</head>
<body>
<header>
  <h1>Performance Test Run Summary</h1>
  <p>{run_name} &middot; {generated_at}</p>
</header>
<div class="container">
<table>
<tr><th>Test</th><th>Result</th><th>Report</th></tr>
{rows}
</table>
</div>
</body>
</html>
"""


def load_config(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def create_result_dir(run_name):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_dir = RESULTS_ROOT / f"run_{run_name}_{timestamp}"
    result_dir.mkdir(parents=True, exist_ok=True)
    return result_dir


def process_results(raw_results, result_dir):
    """Evaluate thresholds and build a JSON + HTML report for each test."""
    processed = []
    for r in raw_results:
        name = r["name"]
        stats_csv = r.get("stats_csv")
        thresholds_file = r.get("thresholds_file")

        evaluator = ThresholdEvaluator(thresholds_file)
        if stats_csv and Path(stats_csv).exists():
            threshold_result = evaluator.evaluate(stats_csv)
        else:
            threshold_result = {"passed": False, "checks": [],
                                 "note": "stats CSV not found - test may have failed to start"}

        json_path, _ = build_json_summary(
            result_dir, name, r["csv_prefix"], threshold_result,
            meta={"return_code": r.get("return_code")},
        )
        html_path = build_html_report(result_dir, name, r["csv_prefix"], threshold_result)

        processed.append({
            "name": name,
            "passed": threshold_result["passed"] and r.get("return_code", 0) == 0,
            "html_report": html_path.name,
            "json_summary": json_path.name,
        })
    return processed


def build_index(result_dir, run_name, processed):
    rows = ""
    for p in processed:
        cls = "pass" if p["passed"] else "fail"
        text = "PASSED" if p["passed"] else "FAILED"
        rows += (
            f"<tr><td>{p['name']}</td><td class='{cls}'>{text}</td>"
            f"<td><a href='{p['html_report']}'>View report</a></td></tr>"
        )
    html = INDEX_TEMPLATE.format(
        run_name=run_name,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        rows=rows,
    )
    index_path = result_dir / "index.html"
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(html)
    return index_path


def main():
    parser = argparse.ArgumentParser(description="Locust-based performance test automation framework")
    parser.add_argument("--config", required=True, help="Path to a test-suite YAML config (see config/test_configs/)")
    parser.add_argument("--no-open", action="store_true", help="Don't auto-open the HTML report when done")
    args = parser.parse_args()

    config = load_config(args.config)
    run_name = config.get("run_name", "perf_test")
    tests = config["tests"]
    parallel = config.get("parallel", False)
    max_workers = config.get("max_parallel_workers")

    result_dir = create_result_dir(run_name)
    print(f"[run_tests] Results will be written to: {result_dir}")

    if parallel and len(tests) > 1:
        raw_results = run_tests_in_parallel(tests, result_dir, max_workers=max_workers)
    else:
        raw_results = [_run_single_test(t, result_dir) for t in tests]

    processed = process_results(raw_results, result_dir)
    index_path = build_index(result_dir, run_name, processed)

    print("\n=== Run Summary ===")
    overall_passed = True
    for p in processed:
        status = "PASS" if p["passed"] else "FAIL"
        if not p["passed"]:
            overall_passed = False
        print(f"  [{status}] {p['name']}")
    print(f"\nCombined report: {index_path}")

    if not args.no_open:
        open_report(index_path)

    sys.exit(0 if overall_passed else 1)


if __name__ == "__main__":
    main()
