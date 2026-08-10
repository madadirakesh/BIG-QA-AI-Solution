import unittest
import os
import tempfile
from unittest.mock import patch

import app as app_module


class LicenseSessionTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config.update(TESTING=True, SECRET_KEY="license-session-test-key")
        self.client = app_module.app.test_client()

    @staticmethod
    def _license_state(valid):
        return {
            "valid": valid,
            "status": "valid" if valid else "error",
            "message": "License verifier temporarily unavailable.",
            "licensed_to": "Test User",
            "record": {},
        }

    def _sign_in(self):
        with self.client.session_transaction() as session:
            session["user_id"] = 42
            session["user_name"] = "Test User"

    def _assert_session_preserved(self):
        with self.client.session_transaction() as session:
            self.assertEqual(session.get("user_id"), 42)
            self.assertEqual(session.get("user_name"), "Test User")

    def test_session_uses_renewing_long_lived_cookie(self):
        with patch.dict(os.environ, {"SESSION_LIFETIME_DAYS": ""}):
            self.assertEqual(app_module.get_env_positive_int("SESSION_LIFETIME_DAYS", 7), 7)
        with patch.dict(os.environ, {"SESSION_LIFETIME_DAYS": "invalid"}):
            self.assertEqual(app_module.get_env_positive_int("SESSION_LIFETIME_DAYS", 7), 7)
        self.assertTrue(app_module.app.config["SESSION_PERMANENT"])
        self.assertTrue(app_module.app.config["SESSION_REFRESH_EACH_REQUEST"])
        self.assertTrue(app_module.app.config["SESSION_COOKIE_HTTPONLY"])

    def test_secret_key_is_reused_from_persistent_file_when_env_value_is_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            secret_file = os.path.join(tmpdir, ".flask_secret_key")
            with open(secret_file, "w", encoding="utf-8") as handle:
                handle.write("persisted-secret-key")

            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("FLASK_SECRET_KEY", None)
                with patch.object(app_module, "FLASK_SECRET_KEY_FILE", app_module.Path(secret_file)):
                    self.assertEqual(app_module.get_flask_secret_key(), "persisted-secret-key")
                    self.assertEqual(os.environ.get("FLASK_SECRET_KEY"), "persisted-secret-key")

    def test_upsert_env_values_preserves_existing_secret_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = os.path.join(tmpdir, ".env")
            with open(env_path, "w", encoding="utf-8") as handle:
                handle.write('FLASK_SECRET_KEY = "keep-me"\n')
                handle.write('SESSION_LIFETIME_DAYS = "7"\n')
                handle.write('AI_TOOL = "OLD"\n')

            app_module.upsert_env_values(env_path, {
                "AI_TOOL": "OPENAI",
                "AI_MODEL": "gpt-4.1-mini",
            })

            with open(env_path, "r", encoding="utf-8") as handle:
                contents = handle.read()

            self.assertIn('FLASK_SECRET_KEY = "keep-me"', contents)
            self.assertIn('SESSION_LIFETIME_DAYS = "7"', contents)
            self.assertIn('AI_TOOL = "OPENAI"', contents)
            self.assertIn('AI_MODEL = "gpt-4.1-mini"', contents)

    def test_invalid_license_uses_blocking_shell_and_preserves_authenticated_session(self):
        self._sign_in()
        with patch.object(app_module, "assess_license_state", return_value=self._license_state(False)):
            response = self.client.get("/home")
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 423)
        self.assertIn('class="app-layout"', page)
        self.assertIn('active license-blocking', page)
        self.assertIn('const isLicenseGatePage = true', page)
        self.assertNotIn('window.location.assign(\'/license\')', page)
        self._assert_session_preserved()

    def test_invalid_license_api_response_preserves_authenticated_session(self):
        self._sign_in()
        with patch.object(app_module, "assess_license_state", return_value=self._license_state(False)):
            response = self.client.get("/api/projects")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json().get("redirect"), "/license")
        self._assert_session_preserved()

    def test_authenticated_license_page_uses_standalone_recovery_layout(self):
        self._sign_in()
        with patch.object(app_module, "assess_license_state", return_value=self._license_state(False)):
            response = self.client.get("/license")
        page = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("License verification", page)
        self.assertIn('class="login-container license-screen"', page)
        self.assertNotIn('class="app-layout"', page)
        self.assertIn("your existing session will continue automatically", page)
        self.assertIn("window.location.href = '/home'", page)
        self._assert_session_preserved()

    def test_valid_license_still_requires_login_when_session_is_missing(self):
        with patch.object(app_module, "assess_license_state", return_value=self._license_state(True)):
            response = self.client.get("/home")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.headers["Location"].endswith("/login"))


if __name__ == "__main__":
    unittest.main()
