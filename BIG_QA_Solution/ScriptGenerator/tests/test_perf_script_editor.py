"""
Edit Script dialog of the Performance Test page against the REAL Flask app
(temporary database; login guard and CSRF protection active). Project
discovery and the AI provider are stubbed; everything else is the real code.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

import pytest

from utils import script_editor

ORIGINAL = ("from locust import HttpUser, task\n\n"
            "class Journey(HttpUser):\n"
            "    @task\n"
            "    def login(self):\n"
            "        self.client.post('/login', data={'user': 'a'})\n")


@pytest.fixture(scope="module")
def app_module(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("perf_editor_app")
    os.environ["SCRIPTGENERATOR_PREREQS_INSTALLED"] = "1"
    os.environ["SCRIPTGENERATOR_VENV_READY"] = "1"
    os.environ["TEST_EXECUTION_DATA_DIR"] = str(tmp / "data")
    import db.app_db as app_db
    app_db.DB_PATH = str(tmp / "app.db")
    app_db.init_db()
    app_db.insert_data("INSERT INTO PerformanceDetails (project_name, application_url, project_path) "
                       "VALUES (?, ?, ?)", ("PerfDemo", "http://app.test", str(tmp)))
    if "app" in sys.modules:
        del sys.modules["app"]
    import app as app_mod
    app_mod.app.config["TESTING"] = True
    return app_mod


@pytest.fixture
def perf_dir(app_module):
    folder = app_module._performance_project(perf_id())[1]
    os.makedirs(folder, exist_ok=True)
    return folder


@pytest.fixture
def script(app_module, perf_dir, monkeypatch):
    os.makedirs(os.path.join(perf_dir, "locustfiles"), exist_ok=True)
    path = Path(perf_dir) / "locustfiles" / "journey.py"
    path.write_bytes(ORIGINAL.replace("\n", "\r\n").encode("utf-8"))   # CRLF must survive a save
    monkeypatch.setattr(app_module, "resolve_performance_script",
                        lambda perf_dir, name, tool="": str(path) if name == "journey.py" else "")
    import db.app_db as app_db
    app_db.update_data("DELETE FROM PerformanceScriptVersions")
    return path


@pytest.fixture
def client(app_module):
    c = app_module.app.test_client()
    with c.session_transaction() as s:
        s["user_id"] = 1
        s["user_role"] = "qa"
        s["user_name"] = "Tester"
    return c


def perf_id():
    import db.app_db as app_db
    return app_db.fetch_data("SELECT id FROM PerformanceDetails WHERE project_name = 'PerfDemo'")[0]["id"]


def csrf(client):
    html = client.get("/qa/performance-test").get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([^"]+)"', html).group(1)


def post(client, url, body):
    return client.post(url, json=body, headers={"X-CSRFToken": csrf(client)})


def test_page_has_editor_dialog(client):
    html = client.get("/qa/performance-test").get_data(as_text=True)
    for element_id in ("perfScriptEditorModal", "scriptEditorText", "scriptAiPrompt",
                       "scriptVersionSelect", "scriptChangeDescription", "scriptEditorSaveBtn"):
        assert f'id="{element_id}"' in html
    assert "openScriptEditor(this)" in html


def test_content_unknown_script_is_rejected(client, script):
    r = client.get(f"/api/performance-test/script/content?perf_id={perf_id()}&script=../../etc/passwd")
    assert r.status_code == 404


def test_save_versions_and_restore(client, script):
    pid = perf_id()
    d = client.get(f"/api/performance-test/script/content?perf_id={pid}&script=journey.py").get_json()
    assert d["content"] == ORIGINAL and d["versions"] == []

    edited = ORIGINAL.replace("data=", "json=")
    d = post(client, "/api/performance-test/script/save",
             {"perf_id": pid, "script": "journey.py", "content": edited, "description": "Login body sent as JSON"}).get_json()
    assert d["status"] == "success" and d["version_no"] == 2
    assert [v["description"] for v in d["versions"]] == ["Original script", "Login body sent as JSON"]
    assert [v["current"] for v in d["versions"]] == [False, True]
    assert script.read_bytes() == edited.replace("\n", "\r\n").encode("utf-8")

    # Invalid Python is refused and the file is left alone.
    r = post(client, "/api/performance-test/script/save",
             {"perf_id": pid, "script": "journey.py", "content": "def broken(:\n"})
    assert r.status_code == 400 and "syntax" in r.get_json()["message"]
    assert script.read_bytes() == edited.replace("\n", "\r\n").encode("utf-8")

    # Version 1 loads back and saves as a new version with a generated description.
    v1 = client.get(f"/api/performance-test/script/version?perf_id={pid}&script=journey.py&version=1").get_json()
    assert v1["version"]["content"] == ORIGINAL
    d = post(client, "/api/performance-test/script/save",
             {"perf_id": pid, "script": "journey.py", "content": v1["version"]["content"]}).get_json()
    assert d["version_no"] == 3 and d["versions"][-1]["description"].startswith("Manual edit")
    assert [v["current"] for v in d["versions"]] == [False, False, True]

    d = post(client, "/api/performance-test/script/save",
             {"perf_id": pid, "script": "journey.py", "content": ORIGINAL}).get_json()
    assert d.get("unchanged") is True


def test_outside_change_is_kept_in_history(client, script):
    pid = perf_id()
    post(client, "/api/performance-test/script/save",
         {"perf_id": pid, "script": "journey.py", "content": ORIGINAL + "\n# one\n", "description": "one"})
    script.write_text(ORIGINAL + "\n# outside\n", encoding="utf-8")
    d = post(client, "/api/performance-test/script/save",
             {"perf_id": pid, "script": "journey.py", "content": ORIGINAL + "\n# two\n", "description": "two"}).get_json()
    assert [v["description"] for v in d["versions"]] == [
        "Original script", "one", "Changes made outside the editor", "two"]


def test_ai_edit_returns_content_without_saving(client, script, app_module, monkeypatch):
    prompts = []

    def fake_ai(prompt):
        prompts.append(prompt)
        return json.dumps({"summary": "Added a Bearer Authorization header to the login request.", "edits": [{
            "find": "self.client.post('/login', data={'user': 'a'})",
            "replace": "self.client.post('/login', data={'user': 'a'}, headers={'Authorization': 'Bearer x'})",
        }]})

    monkeypatch.setattr(app_module, "_api_script_ai_call", fake_ai)
    before = script.read_bytes()
    d = post(client, "/api/performance-test/script/ai-edit", {
        "perf_id": perf_id(), "script": "journey.py", "script_type": "API",
        "prompt": "add an auth header", "content": ORIGINAL}).get_json()
    assert d["status"] == "success"
    for _ in range(50):
        job = client.get(f"/api/performance-test/script/ai-edit/status/{d['job_id']}").get_json()
        if job["status"] != "processing":
            break
        time.sleep(0.05)
    assert job["status"] == "completed", job
    assert "'Authorization': 'Bearer x'" in job["content"]
    assert job["summary"].startswith("Added a Bearer")
    assert "add an auth header" in prompts[0] and "Tool: Locust" in prompts[0]
    assert script.read_bytes() == before


def wait_for(client, job_id):
    for _ in range(100):
        job = client.get(f"/api/performance-test/script/ai-edit/status/{job_id}").get_json()
        if job["status"] != "processing":
            return job
        time.sleep(0.05)
    raise AssertionError("AI job did not finish")


def attach(client, name, data):
    import io
    return client.post("/api/performance-test/script/attachment",
                       data={"perf_id": str(perf_id()), "script": "journey.py", "file": (io.BytesIO(data), name)},
                       headers={"X-CSRFToken": csrf(client)}, content_type="multipart/form-data")


def test_data_file_drives_ai_edit_and_is_saved_with_the_script(client, script, perf_dir, app_module, monkeypatch):
    r = attach(client, "users.csv", b"\xef\xbb\xbfUser,Password\nalice,pw1\nbob,pw2\n")
    assert r.status_code == 200, r.get_json()
    attachment = r.get_json()["attachment"]
    assert attachment["nodes"] == ["User", "Password"] and attachment["row_count"] == 2
    assert attachment["stored_name"] == "journey__users.csv"
    assert attachment["relative_path"].endswith("/journey__users.csv")
    data_file = (Path(script).parent / attachment["relative_path"]).resolve()
    assert not data_file.exists()                       # nothing written before Save

    prompts = []

    def fake_ai(prompt):
        prompts.append(prompt)
        return json.dumps({"summary": "Username now comes from the User column of users.csv.", "edits": [
            {"find": "from locust import HttpUser, task\n",
             "replace": "import csv, itertools, os\nfrom locust import HttpUser, task\n\n"
                        f"USERS = itertools.cycle(list(csv.DictReader(open(os.path.join("
                        f"os.path.dirname(os.path.abspath(__file__)), '{attachment['relative_path']}')))))\n"},
            {"find": "data={'user': 'a'}", "replace": "data={'user': next(USERS)['User']}"},
        ]})

    monkeypatch.setattr(app_module, "_api_script_ai_call", fake_ai)
    d = post(client, "/api/performance-test/script/ai-edit", {
        "perf_id": perf_id(), "script": "journey.py", "content": ORIGINAL,
        "prompt": "map the user field to the User column", "attachments": [attachment["id"]]}).get_json()
    job = wait_for(client, d["job_id"])
    assert job["status"] == "completed", job
    assert attachment["relative_path"] in prompts[0]
    assert "User, Password" in prompts[0] and '"User": "alice"' in prompts[0]
    assert "os.path.abspath(__file__)" in prompts[0]          # Locust guidance for reading files

    d = post(client, "/api/performance-test/script/save", {
        "perf_id": perf_id(), "script": "journey.py", "content": job["content"],
        "description": job["summary"], "attachments": [attachment["id"]]}).get_json()
    assert d["status"] == "success" and "journey__users.csv" in d["message"]
    assert data_file.read_bytes() == b"User,Password\nalice,pw1\nbob,pw2\n"   # BOM stripped
    assert "next(USERS)['User']" in script.read_text(encoding="utf-8")


def test_unknown_attachment_blocks_save(client, script):
    before = script.read_bytes()
    r = post(client, "/api/performance-test/script/save", {
        "perf_id": perf_id(), "script": "journey.py", "content": ORIGINAL + "\n# x\n", "attachments": ["nope"]})
    assert r.status_code == 400 and "attach it again" in r.get_json()["message"]
    assert script.read_bytes() == before


def test_attachment_type_and_content_are_checked(client, script):
    assert attach(client, "notes.txt", b"hello").status_code == 400
    r = attach(client, "bad.json", b"{not json")
    assert r.status_code == 400 and "bad.json" in r.get_json()["message"]


def test_jmeter_gets_json_as_csv():
    raw = json.dumps([{"user": {"name": "alice"}, "id": 1}, {"user": {"name": "bob"}, "id": 2}]).encode()
    a = script_editor.prepare_attachment(raw, "people.json", "plan.jmx", "Jmeter")
    assert a["stored_type"] == "csv" and a["stored_name"] == "plan__people.csv"
    lines = a["content"].decode().splitlines()
    assert lines[0].split(",") == a["nodes"] and len(lines) == 3 and "alice" in lines[1]
    # Locust keeps the file as uploaded.
    assert script_editor.prepare_attachment(raw, "people.json", "s.py", "Locust")["content"] == raw


def test_jmx_validation():
    assert script_editor.validate("plan.jmx", "<jmeterTestPlan><hashTree/></jmeterTestPlan>") == ""
    assert "well-formed" in script_editor.validate("plan.jmx", "<jmeterTestPlan><hashTree></jmeterTestPlan>")
