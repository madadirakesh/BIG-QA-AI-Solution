"""
Payload Configuration end-to-end test.

Drives the dialog's endpoints (config -> upload -> save -> reopen -> clear)
against a real scaffolded performance project in a temp folder and a throwaway
SQLite database, and checks the artefact that matters: the parameterised copy of
the script the runner will execute. The response-time thresholds saved from the
same dialog are covered too, including the case where they are the only thing
configured.

    python -m unittest tests.test_payload_config_flow      (from ScriptGenerator/)
"""

import ast
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db.app_db as app_db

# The DB path has to be swapped before app.py imports fetch_data and friends,
# so the app module is imported inside setUpClass instead of at module scope.
SCRIPT_SOURCE = '''"""
rec_shop.py
-----------
Test Case: Recorded — Shop
"""

from locust import HttpUser, task, between


class ShopUser(HttpUser):
    host = "https://shop.example.com"
    wait_time = between(1, 3)

    @task
    def recorded_journey(self):
        # Step 1 - log in
        self.client.post('/api/login', json={'email': 'bob@example.com', 'tenant': 'acme'},
                         name='/api/login')

        # Step 2 - search
        self.client.get('/search?q=shoes&page=1', name='/search')
'''

PAYLOAD_CSV = "username,password,user_id\nperf_user1,pw1,1001\nperf_user2,pw2,1002\n"


class PayloadConfigurationFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.work = Path(tempfile.mkdtemp(prefix="payload_flow_"))
        app_db.DB_PATH = str(cls.work / "test.db")
        app_db.init_db()

        import app as web
        from utils.performance_scaffolder import scaffold_performance_project

        cls.web = web
        cls.projects = cls.work / "projects"
        cls.projects.mkdir(parents=True, exist_ok=True)
        scaffold_performance_project("Shop", str(cls.projects), "https://shop.example.com", 10, 2, 5)
        cls.perf_dir = cls.projects / "Shop_perf"
        (cls.perf_dir / "locustfiles" / "rec_shop.py").write_text(SCRIPT_SOURCE, encoding="utf-8")

        app_db.insert_data(
            "INSERT INTO PerformanceDetails (project_name, application_url, project_path, "
            "concurrent_user_count, spawn_rate, run_duration) VALUES (?, ?, ?, ?, ?, ?)",
            ("Shop", "https://shop.example.com", str(cls.projects), 10, 2, 5),
        )
        cls.perf_id = app_db.fetch_data("SELECT id FROM PerformanceDetails")[0]["id"]

        web.app.config["WTF_CSRF_ENABLED"] = False
        web.app.config["TESTING"] = True
        cls.client = web.app.test_client()
        with cls.client.session_transaction() as flask_session:
            flask_session["user_id"] = 1
            flask_session["user_role"] = "admin"

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.work, ignore_errors=True)

    # ── helpers ─────────────────────────────────────────────────────────────

    def _config(self):
        return self.client.get("/api/performance-test/payload/config",
                               query_string={"perf_id": self.perf_id, "script": "rec_shop.py"})

    def _upload(self, content, file_name, payload_type):
        return self.client.post("/api/performance-test/payload/upload", data={
            "perf_id": str(self.perf_id), "script": "rec_shop.py", "payload_type": payload_type,
            "file": (io.BytesIO(content), file_name),
        }, content_type="multipart/form-data")

    def _save(self, upload_id, mappings, thresholds=None):
        return self.client.post("/api/performance-test/payload/save", json={
            "perf_id": self.perf_id, "script": "rec_shop.py",
            "upload_id": upload_id, "mappings": mappings,
            "thresholds": thresholds or [],
        })

    def _clear(self):
        return self.client.post("/api/performance-test/payload/clear",
                                json={"perf_id": self.perf_id, "script": "rec_shop.py"})

    @property
    def _generated(self):
        return self.perf_dir / "locustfiles" / "_bigqa_param_rec_shop.py"

    # ── the flow ────────────────────────────────────────────────────────────

    def test_payload_configuration_flow(self):
        with self.subTest("the dialog opens with the script's parameters and no configuration"):
            body = self._config().get_json()
            ids = [parameter["id"] for parameter in body["parameters"]]
            self.assertIn("body::POST /api/login::email", ids)
            self.assertIn("body::POST /api/login::tenant", ids)
            self.assertIn("query::GET /search::q", ids)
            self.assertIsNone(body["config"])

        with self.subTest("a file that does not match the payload type is rejected"):
            response = self._upload(b'{"a": 1}', "orders.json", "csv")
            self.assertEqual(response.status_code, 400)
            self.assertIn("CSV", response.get_json()["message"])

        with self.subTest("a valid CSV parses into payload nodes"):
            response = self._upload(PAYLOAD_CSV.encode(), "users.csv", "csv")
            body = response.get_json()
            self.assertEqual(body["payload"]["nodes"], ["username", "password", "user_id"])
            self.assertEqual(body["payload"]["row_count"], 2)
            upload_id = body["upload_id"]

        with self.subTest("an upload writes nothing into the project before Submit"):
            self.assertFalse(list((self.perf_dir / "data").glob("bigqa_*")))

        with self.subTest("the same script parameter cannot be mapped twice"):
            response = self._save(upload_id, [
                {"parameter": "body::POST /api/login::email", "node": "username"},
                {"parameter": "body::POST /api/login::email", "node": "user_id"},
            ])
            self.assertEqual(response.status_code, 400)
            self.assertIn("more than once", response.get_json()["message"])

        with self.subTest("a valid mapping is saved"):
            response = self._save(upload_id, [
                {"parameter": "body::POST /api/login::email", "node": "username"},
                {"parameter": "query::GET /search::q", "node": "user_id"},
            ])
            body = response.get_json()
            self.assertEqual(response.status_code, 200, json.dumps(body)[:400])
            self.assertEqual(body["unmapped"], ["tenant", "page"])
            self.assertTrue((self.perf_dir / "data" / "bigqa_rec_shop__users.csv").is_file())

        with self.subTest("the parameterised copy reads the mapped fields from the payload"):
            source = self._generated.read_text(encoding="utf-8")
            self.assertIn('_bigqa_value(_bigqa_row, "username")', source)
            self.assertIn('quote_plus(str(_bigqa_value(_bigqa_row, "user_id")))', source)
            # One record per iteration, shared by every field of that iteration.
            self.assertEqual(source.count("_bigqa_row = _bigqa_payload.next()"), 1)

        with self.subTest("unmapped fields keep their recorded value"):
            source = self._generated.read_text(encoding="utf-8")
            self.assertIn("'tenant': 'acme'", source)
            self.assertIn("page=1", source)

        with self.subTest("the recorded script is never rewritten"):
            self.assertEqual((self.perf_dir / "locustfiles" / "rec_shop.py").read_text(encoding="utf-8"),
                             SCRIPT_SOURCE)

        with self.subTest("reopening the dialog restores the saved selections"):
            body = self._config().get_json()
            self.assertEqual([m["node"] for m in body["config"]["mappings"]], ["username", "user_id"])
            self.assertEqual(body["payload"]["nodes"], ["username", "password", "user_id"])
            self.assertEqual(body["payload"]["file_name"], "users.csv")

        with self.subTest("the grid reports the payload and hides the generated copy"):
            listed = {item["file_name"]: item
                      for item in self.client.get("/api/performance-test/scripts",
                                                  query_string={"perf_id": self.perf_id}).get_json()["data"]}
            self.assertNotIn("_bigqa_param_rec_shop.py", listed)
            self.assertEqual(listed["rec_shop.py"]["payload"]["mapping_count"], 2)

        with self.subTest("a run executes the parameterised copy"):
            run_script, prelude, error = self.web._prepare_payload_script(
                self.perf_id, str(self.perf_dir), "rec_shop.py")
            self.assertEqual(error, "")
            self.assertEqual(run_script, "_bigqa_param_rec_shop.py")
            self.assertTrue(any("users.csv" in line for line in prelude))

        with self.subTest("clearing removes the configuration and the generated copy"):
            self.assertEqual(self._clear().get_json()["status"], "success")
            self.assertFalse(self._generated.is_file())
            self.assertEqual(self.web._prepare_payload_script(
                self.perf_id, str(self.perf_dir), "rec_shop.py")[0], "rec_shop.py")

    def test_response_time_threshold_flow(self):
        with self.subTest("the dialog lists the script's requests for the threshold dropdown"):
            body = self._config().get_json()
            self.assertEqual([entry["id"] for entry in body["requests"]],
                             ["POST /api/login", "GET /search"])

        with self.subTest("a threshold needs a positive number of seconds"):
            response = self._save("", [], [{"request": "GET /search", "seconds": 0}])
            self.assertEqual(response.status_code, 400)
            self.assertIn("greater than zero", response.get_json()["message"])

        with self.subTest("a threshold on a request the script does not make is rejected"):
            response = self._save("", [], [{"request": "GET /nope", "seconds": 2}])
            self.assertEqual(response.status_code, 400)
            self.assertIn("no longer a request", response.get_json()["message"])

        with self.subTest("the same request cannot be capped twice"):
            response = self._save("", [], [{"request": "GET /search", "seconds": 2},
                                           {"request": "GET /search", "seconds": 3}])
            self.assertEqual(response.status_code, 400)
            self.assertIn("more than one threshold", response.get_json()["message"])

        with self.subTest("thresholds save on their own, without a payload"):
            response = self._save("", [], [{"request": "__general__", "seconds": 3},
                                           {"request": "GET /search", "seconds": 1.5}])
            body = response.get_json()
            self.assertEqual(response.status_code, 200, json.dumps(body)[:400])
            self.assertEqual(body["config"]["thresholds"],
                             [{"request": "__general__", "seconds": 3.0},
                              {"request": "GET /search", "seconds": 1.5}])

        with self.subTest("the generated copy is valid Python carrying both limits"):
            source = self._generated.read_text(encoding="utf-8")
            ast.parse(source)
            self.assertIn('_BIGQA_THRESHOLD_MS = {"GET /search": 1500.0}', source)
            self.assertIn("_BIGQA_DEFAULT_THRESHOLD_MS = 3000.0", source)
            # No payload was configured, so nothing pulls a record at run time.
            self.assertNotIn("PayloadLoader", source)

        with self.subTest("the run executes the copy and says what it will fail on"):
            run_script, prelude, error = self.web._prepare_payload_script(
                self.perf_id, str(self.perf_dir), "rec_shop.py")
            self.assertEqual(error, "")
            self.assertEqual(run_script, "_bigqa_param_rec_shop.py")
            self.assertTrue(any("General 3s, GET /search 1.5s" in line for line in prelude), prelude)

        with self.subTest("the grid reports the thresholds"):
            listed = {item["file_name"]: item
                      for item in self.client.get("/api/performance-test/scripts",
                                                  query_string={"perf_id": self.perf_id}).get_json()["data"]}
            self.assertEqual(listed["rec_shop.py"]["payload"]["threshold_count"], 2)
            self.assertEqual(listed["rec_shop.py"]["payload"]["mapping_count"], 0)

        with self.subTest("thresholds survive alongside a payload mapping"):
            upload_id = self._upload(PAYLOAD_CSV.encode(), "users.csv", "csv").get_json()["upload_id"]
            response = self._save(upload_id,
                                  [{"parameter": "body::POST /api/login::email", "node": "username"}],
                                  [{"request": "POST /api/login", "seconds": 2}])
            self.assertEqual(response.status_code, 200, response.get_json())
            source = self._generated.read_text(encoding="utf-8")
            ast.parse(source)
            self.assertIn('_bigqa_value(_bigqa_row, "username")', source)
            self.assertIn('_BIGQA_THRESHOLD_MS = {"POST /api/login": 2000.0}', source)
            # No General row this time, so nothing else is capped.
            self.assertIn("_BIGQA_DEFAULT_THRESHOLD_MS = 0.0", source)

        with self.subTest("clearing removes both halves"):
            self.assertEqual(self._clear().get_json()["status"], "success")
            self.assertFalse(self._generated.is_file())


if __name__ == "__main__":
    unittest.main()
