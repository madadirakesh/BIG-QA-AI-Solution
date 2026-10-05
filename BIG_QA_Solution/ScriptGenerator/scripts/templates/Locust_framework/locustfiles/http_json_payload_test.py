"""
http_json_payload_test.py
--------------------------
Sample test demonstrating JSON payload data usage (random selection).

Run via the orchestrator:
    python run_tests.py --config config/test_configs/smoke_test.yaml
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from locust import HttpUser, task, between
from core.payload_loader import PayloadLoader

DATA_FILE = str(Path(__file__).resolve().parent.parent / "data" / "orders.json")
order_loader = PayloadLoader(DATA_FILE, strategy="random")


class OrderUser(HttpUser):
    wait_time = between(1, 2)

    @task
    def place_order(self):
        record = order_loader.next()
        self.client.post("/api/orders", json=record, name="/api/orders [POST-json]")
