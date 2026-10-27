"""
Requirement storing for the Test Case Generator against the REAL Flask app
(temporary database; login guard and CSRF protection active).
"""
import json
import os
import re
import sys

import pytest


@pytest.fixture(scope="module")
def app_module(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("tcg_app")
    os.environ["SCRIPTGENERATOR_PREREQS_INSTALLED"] = "1"   # don't pip install during tests
    os.environ["SCRIPTGENERATOR_VENV_READY"] = "1"
    os.environ["TEST_EXECUTION_DATA_DIR"] = str(tmp / "data")
    import db.app_db as app_db
    app_db.DB_PATH = str(tmp / "app.db")                     # never touch the real database
    app_db.init_db()
    app_db.insert_data("INSERT INTO ProjectDetails (project_name, project_path, project_lang, project_fw, project_tool) "
                       "VALUES (?, ?, ?, ?, ?)", ("Demo", str(tmp), "Python", "Behave", "Selenium"))
    if "app" in sys.modules:
        del sys.modules["app"]
    import app as app_mod
    app_mod.app.config["TESTING"] = True
    return app_mod


@pytest.fixture
def client(app_module):
    c = app_module.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = 1
        s["user_role"] = "qa"
        s["user_name"] = "Tester"
    return c


def csrf(client):
    html = client.get("/qa/test-case-generator").get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def test_page_renders_new_requirement_source_controls(client):
    html = client.get("/qa/test-case-generator").get_data(as_text=True)
    for element_id in ("jiraIncDescription", "jiraIncAC", "jiraIncComments", "jiraIncAttachments",
                       "requirementSourcesPanel", "requirementDocsInput", "jiraImportBtn", "generationNotesSection"):
        assert f'id="{element_id}"' in html
    assert 'id="jiraIncDescription" checked' in html        # existing behaviour stays the default


def test_save_stores_metadata_and_deduplicates(client):
    token = csrf(client)
    import db.app_db as app_db
    pid = app_db.fetch_data("SELECT id FROM ProjectDetails WHERE project_name = 'Demo'")[0]["id"]
    body = {"projectId": pid, "req_name": "Login", "requirement": "===== JIRA TICKET: ABC-1 | Story | Login =====\nx",
            "source_metadata": {"jira_tickets": [{"key": "ABC-1", "information_types": ["Description"]}]}}

    first = client.post("/api/save-project-input", json=body, headers={"X-CSRFToken": token}).get_json()
    assert first["status"] == "success" and first["duplicate"] is False

    again = client.post("/api/save-project-input", json={**body, "req_name": "Other name"},
                        headers={"X-CSRFToken": token}).get_json()
    assert again["duplicate"] is True and again["id"] == first["id"] and again["req_name"] == "Login"

    rows = app_db.fetch_data("SELECT req_name, source_metadata, created_at FROM ProjectInputs WHERE projectId = ?", (pid,))
    assert len(rows) == 1
    assert json.loads(rows[0]["source_metadata"])["jira_tickets"][0]["key"] == "ABC-1"
    assert rows[0]["created_at"]

    listed = client.get(f"/api/project-inputs/{pid}").get_json()["data"]
    assert [r["req_name"] for r in listed] == ["Login"]


def test_save_still_validates_input(client):
    token = csrf(client)
    r = client.post("/api/save-project-input", json={"projectId": None, "req_name": "x", "requirement": "y"},
                    headers={"X-CSRFToken": token})
    assert r.status_code == 400
