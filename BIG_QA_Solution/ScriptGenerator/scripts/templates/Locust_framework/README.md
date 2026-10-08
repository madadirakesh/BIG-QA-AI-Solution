# Locust Performance Test Automation Framework

A Locust-based framework for running all common types of performance
tests (load, stress, spike, soak, smoke) with parallel execution,
multi-protocol support, distributed load generation, CSV/JSON/XML test
data, auth handling, and automatic pass/fail reporting.

## Features

| Feature | Details |
|---|---|
| **Test types** | Load, stress, spike, soak/endurance, smoke — pick a config, tune users/spawn-rate/duration |
| **Parallel execution** | Run multiple test scripts concurrently against the same or different targets |
| **Distributed / multi-machine** | Any test can run as 1 master + N Locust workers, locally or across machines |
| **Protocols** | HTTP/HTTPS out of the box; sample WebSocket and gRPC tests show how to plug in any protocol |
| **Payload data** | CSV, JSON, and XML loaders with round-robin or random per-user selection |
| **Auth handling** | None, Basic, static API key, static bearer, login-based bearer (auto refresh), OAuth2 client-credentials |
| **Thresholds / pass-fail** | YAML-defined limits on error rate, avg/p95/p99 response time, min RPS — global and per-request |
| **Reporting** | Per-test JSON summary + styled HTML report, plus a combined run index; auto-opens in your browser |
| **Timestamped results** | Every run gets its own `results/run_<name>_<YYYYMMDD_HHMMSS>/` folder |

## Project Structure

```
perf_framework/
├── run_tests.py              # main CLI orchestrator
├── requirements.txt
├── config/
│   ├── auth_config.yaml      # auth type + credentials
│   ├── thresholds.yaml       # pass/fail criteria
│   └── test_configs/         # one YAML per test suite (load/stress/spike/soak/smoke)
├── locustfiles/
│   ├── http_api_test.py          # HTTP + CSV payload + auth
│   ├── http_json_payload_test.py # HTTP + JSON payload
│   ├── http_xml_payload_test.py  # HTTP + XML payload
│   ├── websocket_test.py         # non-HTTP protocol example (WebSocket)
│   └── grpc_test.py              # non-HTTP protocol example (gRPC)
├── data/                     # sample CSV / JSON / XML payload files
├── core/
│   ├── payload_loader.py     # CSV/JSON/XML data loading
│   ├── auth_manager.py       # auth handling
│   ├── thresholds.py         # pass/fail evaluation
│   ├── parallel_runner.py    # concurrent test execution
│   ├── distributed_runner.py # master/worker orchestration
│   └── report_generator.py   # HTML/JSON report generation + auto-open
└── results/                  # auto-created, timestamped run folders
```

## Setup

When this project is created from the **Performance Configuration** screen the
setup below is already done for you: a `.venv` is created in this folder and
`requirements.txt` (Locust included) is installed into it. Run it by hand only
if you copied the framework in yourself, or to repair the environment.

```bash
cd perf_framework
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

## Running a Test Suite

```bash
python run_tests.py --config config/test_configs/load_test.yaml
```

This will:
1. Create `results/run_load_test_<timestamp>/`
2. Launch every test listed in the config (in parallel, since `parallel: true`)
3. Evaluate thresholds from `config/thresholds.yaml` (or a per-test override)
4. Write `summary.json` + `report.html` per test, plus a combined `index.html`
5. Open `index.html` in your default browser
6. Exit with code `0` if everything passed, `1` if anything failed thresholds (CI-friendly)

Use `--no-open` to suppress the auto browser launch (e.g. in CI):

```bash
python run_tests.py --config config/test_configs/smoke_test.yaml --no-open
```

## Test Types Included

- **`smoke_test.yaml`** — few users, ~1 min, run on every build
- **`load_test.yaml`** — steady expected-peak load, multiple scripts in parallel
- **`stress_test.yaml`** — far beyond peak, distributed across 8 local workers, to find the breaking point
- **`spike_test.yaml`** — near-instant burst of users (high spawn-rate) to test resilience to sudden traffic
- **`soak_test.yaml`** — moderate load sustained for hours to catch leaks/degradation over time

Copy any of these as a starting point for your own suite — just point
`locustfile`/`host` at your target and adjust `users`/`spawn_rate`/`run_time`.

## Writing Your Own Test

```python
from locust import HttpUser, task, between
from core.payload_loader import PayloadLoader
from core.auth_manager import AuthManager

data = PayloadLoader("data/my_data.csv", strategy="round_robin")
auth = AuthManager.from_config("config/auth_config.yaml")

class MyUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        self.record = data.next()
        self.headers = auth.get_headers(self.client)

    @task
    def my_request(self):
        self.client.get(f"/api/thing/{self.record['id']}", headers=self.headers)
```

Then add it to a test-config YAML under `tests:` and run it.

## Non-HTTP Protocols

Locust's core is protocol-agnostic — `locustfiles/websocket_test.py` and
`locustfiles/grpc_test.py` show the pattern: subclass `User` (not
`HttpUser`), wrap your protocol client's calls, and fire
`environment.events.request.fire(...)` with timing/success info so Locust's
stats engine, CSV output, and this framework's reports work exactly as
they do for HTTP. Use the same pattern for MQTT, Kafka, raw TCP/UDP, etc.

## Distributed / Multi-Machine Runs

Set `distributed.enabled: true` on any test in its YAML config:

```yaml
distributed:
  enabled: true
  worker_count: 8       # local worker processes to spawn
  master_bind_port: 5557
```

This spawns 1 master + `worker_count` local worker processes automatically.
To scale across **multiple machines**, run the master locally as above but
also start additional workers on other hosts pointing at the master's IP:

```bash
locust -f locustfiles/http_api_test.py --worker --master-host=<master-ip> --master-port=5557
```

## Auth Handling

Configure once in `config/auth_config.yaml`; every locustfile that calls
`AuthManager.get_headers()` picks it up automatically. Supported types:
`none`, `basic`, `api_key`, `bearer_static`, `bearer_login` (auto
re-login on expiry), `oauth2_client` (client-credentials grant). See
inline comments in the file for each type's required fields.

## Thresholds / Pass-Fail Criteria

Defined in `config/thresholds.yaml` (or a per-test override via
`thresholds_file` in the test config):

```yaml
global:
  max_error_rate_pct: 1.0
  max_p95_response_time_ms: 1200
  min_rps: 5

per_request:
  "/api/orders":
    max_p95_response_time_ms: 900
```

After each run, every configured metric is checked against the actual
Locust stats; results appear in the HTML report and `summary.json`, and
a non-zero exit code is returned if any check fails — ready to gate a
CI/CD pipeline.

## Notes

- `run_time` accepts Locust's duration format: `30s`, `5m`, `4h`, etc.
- Each test run is fully isolated in its own timestamped folder — nothing
  gets overwritten between runs.
- The gRPC sample requires you to generate your own `*_pb2.py` /
  `*_pb2_grpc.py` stubs from your `.proto` file before it can run.
