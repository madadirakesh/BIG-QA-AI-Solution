import unittest
from unittest.mock import patch

import requests

from api import license_service


class LicenseServiceFailClosedTests(unittest.TestCase):
    def setUp(self):
        self.valid_record = {
            "license_key": "TEST-LICENSE",
            "status": "valid",
            "message": "Previously valid",
            "licensed_to": "Test User",
            "last_checked_at": "2026-08-10T00:00:00Z",
        }
        self.error_record = {**self.valid_record, "status": "error"}

    def test_network_failure_blocks_even_when_previous_state_was_valid(self):
        with (
            patch.object(license_service, "get_license_record", side_effect=[self.valid_record, self.error_record]),
            patch.object(
                license_service,
                "call_license_verifier",
                side_effect=requests.ConnectionError("connection refused"),
            ),
            patch.object(license_service, "save_license_record") as save_record,
        ):
            state = license_service.assess_license_state(force_refresh=True)

        self.assertFalse(state["valid"])
        self.assertEqual(state["status"], "error")
        self.assertEqual(state["message"], license_service.LICENSE_SERVICE_UNAVAILABLE_MESSAGE)
        self.assertNotIn("connection refused", state["message"])
        save_record.assert_called_once()
        self.assertEqual(save_record.call_args.args[1], "error")

    def test_transient_http_failure_is_service_error_not_valid_cache(self):
        verification = {
            "valid": False,
            "status_code": 503,
            "message": "Service Unavailable",
            "licensed_to": "",
            "license_period": {},
        }
        with (
            patch.object(license_service, "get_license_record", side_effect=[self.valid_record, self.error_record]),
            patch.object(license_service, "call_license_verifier", return_value=verification),
            patch.object(license_service, "save_license_record") as save_record,
        ):
            state = license_service.assess_license_state(force_refresh=True)

        self.assertFalse(state["valid"])
        self.assertEqual(state["status"], "error")
        self.assertEqual(state["message"], license_service.LICENSE_SERVICE_UNAVAILABLE_MESSAGE)
        self.assertNotIn("503", state["message"])
        save_record.assert_called_once()
        self.assertEqual(save_record.call_args.args[1], "error")

    def test_business_level_invalid_license_remains_invalid(self):
        verification = {
            "valid": False,
            "status_code": 403,
            "message": "License expired",
            "licensed_to": "Test User",
            "license_period": {},
        }
        invalid_record = {**self.valid_record, "status": "invalid"}
        with (
            patch.object(license_service, "get_license_record", side_effect=[self.valid_record, invalid_record]),
            patch.object(license_service, "call_license_verifier", return_value=verification),
            patch.object(license_service, "save_license_record") as save_record,
        ):
            state = license_service.assess_license_state(force_refresh=True)

        self.assertFalse(state["valid"])
        self.assertEqual(state["status"], "invalid")
        self.assertEqual(state["message"], "License expired")
        self.assertEqual(save_record.call_args.args[1], "invalid")

    def test_cached_service_error_hides_previous_endpoint_details(self):
        cached_error = {
            **self.error_record,
            "last_checked_at": license_service.utcnow_iso(),
            "message": "Unable to reach endpoint. Tried: http://127.0.0.1:5000/api/licenses/validate",
        }
        with patch.object(license_service, "get_license_record", return_value=cached_error):
            state = license_service.assess_license_state(force_refresh=False)

        self.assertEqual(state["message"], license_service.LICENSE_SERVICE_UNAVAILABLE_MESSAGE)
        self.assertNotIn("http://", state["message"])


if __name__ == "__main__":
    unittest.main()
