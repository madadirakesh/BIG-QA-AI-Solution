"""
http_api_test.py
-----------------
Sample HTTP load test demonstrating:
  - CSV payload data (round-robin per user)
  - Auth handling via AuthManager (bearer_login example)
  - Multiple weighted tasks

Run standalone (from the perf_framework/ root):
    locust -f locustfiles/http_api_test.py --host https://api.example.com

Or via the framework's orchestrator (recommended):
    python run_tests.py --config config/test_configs/load_test.yaml
"""

import sys
from pathlib import Path

# Allow "core" imports when Locust runs this file directly from locustfiles/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from locust import HttpUser, task, between
from core.payload_loader import PayloadLoader
from core.auth_manager import AuthManager

DATA_FILE = str(Path(__file__).resolve().parent.parent / "data" / "users.csv")
AUTH_CONFIG = str(Path(__file__).resolve().parent.parent / "config" / "auth_config.yaml")

user_loader = PayloadLoader(DATA_FILE, strategy="round_robin")
auth_manager = AuthManager.from_config(AUTH_CONFIG)


class ApiUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        self.record = user_loader.next()
        # If auth_config.yaml type is "none", this returns {} harmlessly.
        try:
            self.headers = auth_manager.get_headers(self.client)
        except Exception:
            # Login endpoint not reachable in this sample environment;
            # fall back to no auth so the sample still runs standalone.
            self.headers = {}

    @task(3)
    def get_profile(self):
        self.client.get(
            f"/api/users/{self.record.get('user_id', '1000')}",
            headers=self.headers,
            name="/api/users/[id]",
        )

    @task(2)
    def list_orders(self):
        self.client.get("/api/orders", headers=self.headers, name="/api/orders")

    @task(1)
    def create_order(self):
        payload = {"customer_id": self.record.get("user_id", "1000"), "quantity": 1}
        self.client.post(
            "/api/orders",
            json=payload,
            headers=self.headers,
            name="/api/orders [POST]",
        )
