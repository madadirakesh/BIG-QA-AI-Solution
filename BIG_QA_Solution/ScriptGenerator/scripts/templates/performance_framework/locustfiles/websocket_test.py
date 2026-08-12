"""
websocket_test.py
------------------
Demonstrates protocol support BEYOND plain HTTP: a WebSocket load test
built on a custom Locust User (not HttpUser). Locust is protocol-agnostic
at its core - any custom User subclass that fires `request` events works
with the standard Locust UI/stats/reporting.

Requires: pip install websocket-client

Run standalone:
    locust -f locustfiles/websocket_test.py --host wss://echo.example.com

For gRPC, MQTT, Kafka, or raw TCP/UDP protocols, follow the same pattern:
wrap the protocol client's calls with self.environment.events.request.fire(...)
so Locust captures timing/success/failure exactly like it does for HTTP.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from locust import User, task, between, events

try:
    import websocket
except ImportError:
    websocket = None  # only required if this test is actually executed


class WebSocketClient:
    """Thin wrapper that reports timings back to Locust's stats engine."""

    def __init__(self, host, environment):
        self.host = host
        self.environment = environment
        self.ws = None

    def connect(self):
        start = time.time()
        try:
            self.ws = websocket.create_connection(self.host, timeout=10)
            self._fire("connect", "websocket_connect", start, success=True)
        except Exception as e:
            self._fire("connect", "websocket_connect", start, success=False, exception=e)
            raise

    def send_and_receive(self, message, name="websocket_message"):
        start = time.time()
        try:
            self.ws.send(message)
            response = self.ws.recv()
            self._fire("send", name, start, success=True, response_length=len(response))
            return response
        except Exception as e:
            self._fire("send", name, start, success=False, exception=e)
            raise

    def _fire(self, request_type, name, start_time, success, exception=None, response_length=0):
        total_time = (time.time() - start_time) * 1000
        self.environment.events.request.fire(
            request_type=request_type,
            name=name,
            response_time=total_time,
            response_length=response_length,
            exception=None if success else exception,
            context={},
        )

    def close(self):
        if self.ws:
            self.ws.close()


class WebSocketUser(User):
    """
    A custom protocol User. `wait_time` and `@task` work exactly as with
    HttpUser; Locust doesn't care what protocol you actually speak.
    """
    wait_time = between(1, 3)

    def on_start(self):
        self.client = WebSocketClient(self.host, self.environment)
        self.client.connect()

    def on_stop(self):
        self.client.close()

    @task
    def echo_message(self):
        self.client.send_and_receive('{"type": "ping"}', name="ws_ping")
