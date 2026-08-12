"""
parallel_runner.py
-------------------
Runs one or more configured tests concurrently. Each test can itself be
standalone (single Locust process) or distributed (master + local workers).

Tests run in a ThreadPoolExecutor since each test is really just a
subprocess we wait on — threads are sufficient and keep things simple to
manage/log.
"""

import sys
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from core.distributed_runner import run_distributed


def build_standalone_command(locustfile, host, users, spawn_rate, run_time,
                              csv_prefix, html_out, extra_args=None):
    cmd = [
        sys.executable, "-m", "locust",
        "-f", locustfile,
        "--host", host,
        "--headless",
        "--users", str(users),
        "--spawn-rate", str(spawn_rate),
        "--run-time", run_time,
        "--csv", csv_prefix,
        "--html", html_out,
        "--only-summary",
    ]
    if extra_args:
        cmd.extend(extra_args)
    return cmd


def _run_single_test(test_cfg, result_dir):
    """
    test_cfg is a dict parsed from a test's YAML config (see
    config/test_configs/*.yaml). Returns a result dict describing the
    outcome and where its artifacts live.
    """
    name = test_cfg["name"]
    locustfile = test_cfg["locustfile"]
    host = test_cfg["host"]
    users = test_cfg.get("users", 10)
    spawn_rate = test_cfg.get("spawn_rate", 1)
    run_time = test_cfg.get("run_time", "1m")
    extra_args = test_cfg.get("extra_args", [])
    distributed = test_cfg.get("distributed", {}).get("enabled", False)

    csv_prefix = str(result_dir / name)
    html_out = str(result_dir / f"{name}_locust_native.html")

    print(f"[parallel_runner] Launching test '{name}' "
          f"(distributed={distributed}) against {host} ...")

    if distributed:
        worker_count = test_cfg["distributed"].get("worker_count", 4)
        master_bind_port = test_cfg["distributed"].get("master_bind_port", 5557)
        rc = run_distributed(
            locustfile, host, users, spawn_rate, run_time,
            csv_prefix, html_out, worker_count=worker_count,
            master_bind_port=master_bind_port, log_dir=str(result_dir),
            extra_args=extra_args,
        )
    else:
        cmd = build_standalone_command(
            locustfile, host, users, spawn_rate, run_time,
            csv_prefix, html_out, extra_args,
        )
        log_path = result_dir / f"{name}.log"
        with open(log_path, "w") as logf:
            proc = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT)
        rc = proc.returncode

    return {
        "name": name,
        "return_code": rc,
        "csv_prefix": csv_prefix,
        "stats_csv": f"{csv_prefix}_stats.csv",
        "thresholds_file": test_cfg.get("thresholds_file"),
    }


def run_tests_in_parallel(test_configs, result_dir, max_workers=None):
    """
    test_configs : list of test-config dicts
    result_dir   : Path to the timestamped run folder
    max_workers  : max concurrent tests (defaults to len(test_configs))

    Returns a list of per-test result dicts (see _run_single_test).
    """
    max_workers = max_workers or len(test_configs)
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_run_single_test, cfg, result_dir): cfg["name"]
            for cfg in test_configs
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                results.append(future.result())
            except Exception as exc:
                print(f"[parallel_runner] Test '{name}' raised an exception: {exc}")
                results.append({"name": name, "return_code": -1, "error": str(exc)})
    return results
