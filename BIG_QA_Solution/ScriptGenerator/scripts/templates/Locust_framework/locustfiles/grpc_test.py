"""
grpc_test.py
------------
Demonstrates gRPC protocol support using a custom Locust User, following
the same request-instrumentation pattern as websocket_test.py.

Requires: pip install grpcio grpcio-tools, plus your service's generated
*_pb2.py / *_pb2_grpc.py stub files (not included here - generate them
from your .proto file with `python -m grpc_tools.protoc`).

Run standalone:
    locust -f locustfiles/grpc_test.py --host my-grpc-service:50051
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from locust import User, task, between

try:
    import grpc
except ImportError:
    grpc = None  # only required if this test is actually executed

# import your_service_pb2, your_service_pb2_grpc  # <-- generated stubs


class GrpcClient:
    def __init__(self, host, environment):
        self.environment = environment
        self.channel = grpc.insecure_channel(host)
        # self.stub = your_service_pb2_grpc.YourServiceStub(self.channel)

    def call(self, name, fn, *args, **kwargs):
        start = time.time()
        success, exception, response = True, None, None
        try:
            response = fn(*args, **kwargs)
        except grpc.RpcError as e:
            success = False
            exception = e
        finally:
            total_time = (time.time() - start) * 1000
            self.environment.events.request.fire(
                request_type="grpc",
                name=name,
                response_time=total_time,
                response_length=response.ByteSize() if response else 0,
                exception=exception,
                context={},
            )
        return response

    def close(self):
        self.channel.close()


class GrpcUser(User):
    wait_time = between(1, 2)

    def on_start(self):
        self.client = GrpcClient(self.host, self.environment)

    def on_stop(self):
        self.client.close()

    @task
    def call_get_status(self):
        # Example call - replace `self.client.stub.GetStatus` and request
        # message with your actual generated gRPC stub/message types.
        # self.client.call(
        #     "GetStatus",
        #     self.client.stub.GetStatus,
        #     your_service_pb2.StatusRequest(),
        # )
        pass
