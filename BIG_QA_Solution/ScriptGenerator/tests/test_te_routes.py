"""
Routes and page rendering against the REAL Flask app (temporary database and
data directory; login guard and CSRF protection active).
"""
import io
import os
import re
import sys

import pytest


@pytest.fixture(scope="module")
def app_module(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("te_app")
    os.environ["SCRIPTGENERATOR_PREREQS_INSTALLED"] = "1"   # don't pip install during tests
    os.environ["SCRIPTGENERATOR_VENV_READY"] = "1"
    os.environ["TEST_EXECUTION_DATA_DIR"] = str(tmp / "data")
    import db.app_db as app_db
    app_db.DB_PATH = str(tmp / "app.db")                     # never touch the real database
    app_db.init_db()
    if "app" in sys.modules:
        del sys.modules["app"]
    import app as app_mod
    app_mod.app.config["TESTING"] = True
    return app_mod


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


def login(client, role="qa"):
    with client.session_transaction() as s:
        s["user_id"] = 1
        s["user_role"] = role
        s["user_name"] = "Tester"


def csrf(client):
    html = client.get("/qa/test-execution").get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def test_unauthenticated_api_is_rejected(client):
    assert client.get("/api/test-execution/runs").status_code == 401
    assert client.get("/qa/test-execution").status_code == 302


def test_guest_role_is_forbidden(client):
    login(client, "guest")
    assert client.get("/api/test-execution/runs").status_code == 403


def test_page_renders_and_sidebar_order(client):
    login(client)
    html = client.get("/qa/test-execution").get_data(as_text=True)
    assert "Choose test case source" in html and "js/test_execution.js" in html
    nav = html[html.index('class="sidebar-nav"'):]
    gen, exe, loc = nav.index("Test Case Generator"), nav.index("Test Execution"), nav.index("Element Locator")
    assert gen < exe < loc                                    # immediately after Test Case Generator
    page = client.get("/qa/test-case-generator")              # existing page still renders with the new nav item
    assert page.status_code == 200 and "/qa/test-execution" in page.get_data(as_text=True)


@pytest.mark.parametrize("path", ["/home", "/configurations", "/qa/test-case-generator", "/qa/script-developer",
                                  "/qa/script-runner", "/qa/performance-test", "/qa/performance-dashboard"])
def test_existing_pages_still_render(client, path):
    login(client, "admin")
    r = client.get(path)
    assert r.status_code == 200, path
    assert "Test Execution" in r.get_data(as_text=True)      # shared sidebar renders the new item everywhere


def test_post_without_csrf_is_rejected(client):
    login(client)
    r = client.post("/api/test-execution/runs", json={})
    assert r.status_code == 400 and "CSRF" in r.get_json()["message"]


def test_upload_validation_and_preview(client):
    login(client)
    token = csrf(client)
    bad = client.post("/api/test-execution/upload", headers={"X-CSRFToken": token},
                      data={"file": (io.BytesIO(b"<script>"), "evil.html")}, content_type="multipart/form-data")
    assert bad.status_code == 400

    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.append(["Test Case ID", "Title", "Test Steps", "Expected Result"])
    wb.active.append(["TC-1", "Open", "1. Open the application\n2. Click on Login button", "2. 'Welcome' is displayed"])
    buf = io.BytesIO()
    wb.save(buf)
    up = client.post("/api/test-execution/upload", headers={"X-CSRFToken": token},
                     data={"file": (io.BytesIO(buf.getvalue()), "../../cases.xlsx")}, content_type="multipart/form-data")
    body = up.get_json()
    assert up.status_code == 200 and body["filename"] == "cases.xlsx"
    sheet = body["sheets"][0]
    prev = client.post("/api/test-execution/excel/preview", headers={"X-CSRFToken": token},
                       json={"upload_id": body["upload_id"], "sheet": sheet["name"], "mapping": sheet["suggested_mapping"]})
    data = prev.get_json()
    assert prev.status_code == 200 and data["stats"]["total"] == 1 and data["stats"]["steps_needing_ai"] == 0


def test_run_requires_base_url(client):
    login(client)
    token = csrf(client)
    r = client.post("/api/test-execution/runs", headers={"X-CSRFToken": token},
                    json={"source": "jira", "ref": "PROJ-1", "config": {}})
    assert r.status_code in (400, 502)


def test_bad_ids_and_missing_runs(client):
    login(client)
    assert client.get("/api/test-execution/runs/..%2F..%2Fapp").status_code == 404
    assert client.get("/api/test-execution/runs/TE-20261023-00000000").status_code == 404
    assert client.get("/api/test-execution/runs/TE-20261023-00000000/evidence/..").status_code == 404


def test_zephyr_config_rejects_injection(client, app_module, tmp_path, monkeypatch):
    login(client)
    token = csrf(client)
    r = client.post("/api/test-execution/zephyr/config", headers={"X-CSRFToken": token},
                    json={"api_token": 'x"\nAPI_KEY="stolen', "base_url": ""})
    assert r.status_code == 400
    r = client.post("/api/test-execution/zephyr/config", headers={"X-CSRFToken": token},
                    json={"api_token": "", "base_url": "http://insecure"})
    assert r.status_code == 400
