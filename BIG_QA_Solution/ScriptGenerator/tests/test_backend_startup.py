import unittest
from unittest.mock import MagicMock, patch

import app as app_module


class BackendStartupTests(unittest.TestCase):
    def test_existing_healthy_backend_is_reused(self):
        response = MagicMock()
        response.status = 200
        response.__enter__.return_value = response
        response.__exit__.return_value = False

        with (
            patch("urllib.request.urlopen", return_value=response),
            patch.object(app_module.subprocess, "Popen") as popen,
        ):
            result = app_module.launch_backend()

        self.assertIsNone(result)
        popen.assert_not_called()

    def test_backend_is_started_when_health_endpoint_is_unavailable(self):
        process = MagicMock()
        with (
            patch("urllib.request.urlopen", side_effect=OSError("not running")),
            patch.object(app_module.subprocess, "Popen", return_value=process) as popen,
        ):
            result = app_module.launch_backend()

        self.assertIs(result, process)
        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertIn("api.backend:app", command)
        self.assertEqual(command[command.index("--port") + 1], "8000")


if __name__ == "__main__":
    unittest.main()
