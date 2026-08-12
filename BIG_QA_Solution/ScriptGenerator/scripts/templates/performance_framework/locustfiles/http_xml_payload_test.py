"""
http_xml_payload_test.py
-------------------------
Sample test demonstrating XML payload data usage, converting each record
to an XML request body.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from locust import HttpUser, task, between
from core.payload_loader import PayloadLoader

DATA_FILE = str(Path(__file__).resolve().parent.parent / "data" / "payments.xml")
payment_loader = PayloadLoader(DATA_FILE, strategy="round_robin")


def to_xml_body(record):
    return (
        "<payment>"
        f"<account_id>{record['account_id']}</account_id>"
        f"<amount>{record['amount']}</amount>"
        f"<currency>{record['currency']}</currency>"
        "</payment>"
    )


class PaymentUser(HttpUser):
    wait_time = between(1, 2)

    @task
    def submit_payment(self):
        record = payment_loader.next()
        body = to_xml_body(record)
        self.client.post(
            "/api/payments",
            data=body,
            headers={"Content-Type": "application/xml"},
            name="/api/payments [POST-xml]",
        )
