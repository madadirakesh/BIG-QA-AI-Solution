"""
distributed_runner.py
----------------------
Handles running a single Locust test in distributed (master/worker) mode
across multiple processes and, optionally, multiple machines.

Local distributed mode: spawns 1 master + N worker subprocesses on this
machine, each worker in its own process, communicating over Locust's
built-in --master-bind-port (default 5557).

Remote distributed mode: this machine runs the master only; worker
machines are expected to run:
    locust -f <locustfile> --worker --master-host=<this-machine-ip>
(You point `worker_hosts` at machines you SSH into separately, or use a
provisioning tool — this framework just starts the master and waits.)
"""

import subprocess
import sys
import time


def build_master_command(locustfile, host, users, spawn_rate, run_time,
                          csv_prefix, html_out, extra_args=None,
                          master_bind_port=5557, expect_workers=1):
    cmd = [
        sys.executable, "-m", "locust",
        "-f", locustfile,
        "--host", host,
        "--master",
        "--master-bind-port", str(master_bind_port),
        "--expect-workers", str(expect_workers),
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


def build_worker_command(locustfile, master_host="127.0.0.1", master_port=5557, extra_args=None):
    cmd = [
        sys.executable, "-m", "locust",
        "-f", locustfile,
        "--worker",
        "--master-host", master_host,
        "--master-port", str(master_port),
    ]
    if extra_args:
        cmd.extend(extra_args)
    return cmd


def run_distributed(locustfile, host, users, spawn_rate, run_time,
                     csv_prefix, html_out, worker_count=4,
                     master_bind_port=5557, log_dir=None, extra_args=None):
    """
    Spawns `worker_count` local worker processes plus one master process,
    waits for the master (test) to finish, then cleans up workers.

    Returns the master process return code.
    """
    log_dir = log_dir or "."
    worker_procs = []

    print(f"[distributed_runner] Starting {worker_count} local worker(s)...")
    for i in range(worker_count):
        w_cmd = build_worker_command(locustfile, master_host="127.0.0.1", master_port=master_bind_port)
        w_log = open(f"{log_dir}/worker_{i}.log", "w")
        proc = subprocess.Popen(w_cmd, stdout=w_log, stderr=subprocess.STDOUT)
        worker_procs.append((proc, w_log))

    # give workers a moment to boot before starting the master
    time.sleep(2)

    master_cmd = build_master_command(
        locustfile, host, users, spawn_rate, run_time,
        csv_prefix, html_out, extra_args,
        master_bind_port=master_bind_port, expect_workers=worker_count,
    )
    print(f"[distributed_runner] Starting master: {' '.join(master_cmd)}")
    master_result = subprocess.run(master_cmd)

    print("[distributed_runner] Test complete, terminating workers...")
    for proc, w_log in worker_procs:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        w_log.close()

    return master_result.returncode
