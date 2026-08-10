import unittest
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
