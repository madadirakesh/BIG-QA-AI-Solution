"""
thresholds.py
--------------
Evaluates pass/fail criteria against the stats produced by a Locust run.

Thresholds are defined in YAML, e.g. config/thresholds.yaml:

    global:
      max_error_rate_pct: 1.0
      max_avg_response_time_ms: 500
      max_p95_response_time_ms: 1200

    per_request:
      "GET /api/orders":
        max_p95_response_time_ms: 800
        max_error_rate_pct: 0.5

Locust's `--csv` flag produces a `<prefix>_stats.csv` file with one row per
request name plus an "Aggregated" row. We parse that file and compare it
against the configured thresholds.
"""

import csv
import yaml


class ThresholdEvaluator:
    def __init__(self, thresholds_config_path=None):
        self.thresholds = {"global": {}, "per_request": {}}
        if thresholds_config_path:
            with open(thresholds_config_path, encoding="utf-8") as f:
                loaded = yaml.safe_load(f) or {}
            self.thresholds["global"] = loaded.get("global", {})
            self.thresholds["per_request"] = loaded.get("per_request", {})

    def evaluate(self, stats_csv_path):
        """
        Reads a Locust *_stats.csv file and returns:
            {
              "passed": bool,
              "checks": [ {name, metric, limit, actual, passed}, ... ]
            }
        """
        rows = self._read_stats(stats_csv_path)
        checks = []

        for row in rows:
            name = row["Name"]
            is_aggregated = name.strip().lower() == "aggregated"
            rules = dict(self.thresholds["global"]) if is_aggregated else {}
            rules.update(self.thresholds["per_request"].get(name, {}))
            if not rules:
                continue

            requests_count = float(row.get("Request Count", 0) or 0)
            failures = float(row.get("Failure Count", 0) or 0)
            error_rate = (failures / requests_count * 100) if requests_count else 0.0

            metric_map = {
                "max_error_rate_pct": error_rate,
                "max_avg_response_time_ms": float(row.get("Average Response Time", 0) or 0),
                "max_p95_response_time_ms": float(row.get("95%", 0) or 0),
                "max_p99_response_time_ms": float(row.get("99%", 0) or 0),
                "min_rps": float(row.get("Requests/s", 0) or 0),
            }

            for metric, limit in rules.items():
                if metric not in metric_map:
                    continue
                actual = metric_map[metric]
                if metric == "min_rps":
                    passed = actual >= limit
                else:
                    passed = actual <= limit
                checks.append({
                    "name": name,
                    "metric": metric,
                    "limit": limit,
                    "actual": round(actual, 2),
                    "passed": passed,
                })

        overall_passed = all(c["passed"] for c in checks) if checks else True
        return {"passed": overall_passed, "checks": checks}

    @staticmethod
    def _read_stats(stats_csv_path):
        with open(stats_csv_path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
