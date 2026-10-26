"""
LIVE integration test: real @playwright/mcp server + real headless browser against
a local demo page. Skipped when Node.js/npx is missing or TE_SKIP_LIVE=1.
(Jira and Zephyr have no live test: no test instance is configured here.)
"""
import functools
import http.server
import io
import os
import shutil
import threading
import time

import openpyxl
import pytest

from test_execution.models import RunConfig
from test_execution.service import TestExecutionService

pytestmark = pytest.mark.skipif(
    not (shutil.which("npx") or shutil.which("npx.cmd")) or os.environ.get("TE_SKIP_LIVE") == "1",
    reason="Playwright MCP needs Node.js/npx (set TE_SKIP_LIVE=1 to skip)")

DEMO = os.path.join(os.path.dirname(__file__), "fixtures", "demo_app")


@pytest.fixture(scope="module")
def demo_url():
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=DEMO)
    handler.log_message = lambda *a: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/index.html"
    srv.shutdown()


def test_live_excel_run_through_playwright_mcp(tmp_path, demo_url):
    wb = openpyxl.Workbook()
    for row in [
        ["Test Case ID", "Title", "Test Steps", "Expected Result"],
        ["TC-LIVE-1", "Valid login", "1. Navigate to the login page\n2. Enter valid username\n3. Enter valid password\n"
                                     "4. Select 'Admin' from Role dropdown\n5. Check the Remember me checkbox\n6. Click on Login button",
         "1. Page title is 'Demo Login'\n6. 'Welcome back' message is displayed"],
        ["TC-LIVE-2", "Wrong password", "1. Open the application\n2. Enter 'alice' in Username field\n"
                                        "3. Enter 'wrong' in Password field\n4. Click on Login button",
         "4. 'Welcome back' message is displayed"],
        ["TC-LIVE-3", "Off-limits host", "1. Navigate to https://example.org/", ""],
    ]:
        wb.active.append(row)
    buf = io.BytesIO()
    wb.save(buf)

    svc = TestExecutionService(str(tmp_path / "data"), str(tmp_path / "db.sqlite"), ai_call=None)
    up = svc.save_upload("qa", "live.xlsx", buf.getvalue())
    sheet = up["sheets"][0]
    cases, meta = svc.excel_cases(up["upload_id"], sheet["name"], sheet["suggested_mapping"])
    cfg = RunConfig.from_dict({"base_url": demo_url, "screenshot_mode": "every_step", "action_timeout_ms": 4000,
                               "use_ai_planner": False, "use_ai_locator_recovery": False})
    run_id = svc.start_run("qa", "excel", cases, cfg, meta, {"username": "alice", "password": "s3cret!"})
    deadline = time.time() + 300
    while svc.is_active(run_id):
        assert time.time() < deadline
        time.sleep(0.5)
    view = svc.run_view(run_id)
    by = {r["test_key"]: r for r in view["results"]}
    assert by["TC-LIVE-1"]["status"] == "Passed", by["TC-LIVE-1"]
    assert by["TC-LIVE-2"]["status"] == "Failed" and by["TC-LIVE-2"]["failed_step"] == 4
    assert by["TC-LIVE-3"]["status"] == "Blocked" and by["TC-LIVE-3"]["failure_category"] == "policy"
    assert view["usage"]["model_calls"] == 0 and view["usage"]["browser_operations"] > 0
    shots = by["TC-LIVE-1"]["evidence"]
    assert len(shots) >= 3
    png = svc.evidence_bytes(run_id, shots[0]["evidence_id"])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    import json
    assert "s3cret!" not in json.dumps(view)
    out = openpyxl.load_workbook(svc.output_path(run_id))
    assert out["Execution Evidence"]._images
