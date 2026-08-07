import os
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ScriptRunnerEngine.runner import ScriptRunnerService


class ReportMappingTests(unittest.TestCase):
    STACK_REPORTS = (
        ("selenium-python", "Results/report.html"),
        ("playwright-python", "Results/report.html"),
        ("selenium-java", "Results/cucumber.html"),
        ("playwright-java", "Results/cucumber.html"),
        ("selenium-csharp", "Results/report.html"),
        ("playwright-typescript", "Results/2026-08-07/cucumber_report.html"),
    )

    @staticmethod
    def _selected_path(report_url):
        return Path(parse_qs(urlparse(report_url).query)["path"][0])

    def test_each_stack_selects_only_its_fresh_current_report(self):
        for stack, relative_report in self.STACK_REPORTS:
            with self.subTest(stack=stack), tempfile.TemporaryDirectory(prefix=f"{stack}-") as tmp:
                root = Path(tmp)
                stale = root / "Results" / "old_report.html"
                stale.parent.mkdir(parents=True)
                stale.write_text("stale", encoding="utf-8")
                old_time = time.time() - 60
                os.utime(stale, (old_time, old_time))

                started_at = time.time()
                time.sleep(0.005)
                expected = root / relative_report
                expected.parent.mkdir(parents=True, exist_ok=True)
                expected.write_text(f"<html>{stack} current report</html>", encoding="utf-8")

                report_url = ScriptRunnerService._current_report_url(tmp, started_at, True, "passed")
                self.assertEqual(self._selected_path(report_url), expected)
                self.assertIn("run", parse_qs(urlparse(report_url).query))

    def test_each_stack_uses_fresh_diagnostic_report_after_early_failure(self):
        for stack, relative_report in self.STACK_REPORTS:
            with self.subTest(stack=stack), tempfile.TemporaryDirectory(prefix=f"{stack}-") as tmp:
                root = Path(tmp)
                previous = root / relative_report
                previous.parent.mkdir(parents=True, exist_ok=True)
                previous.write_text("previous run", encoding="utf-8")

                started_at = time.time()
                time.sleep(0.005)
                report_url = ScriptRunnerService._current_report_url(
                    tmp, started_at, False, f"{stack} current failure"
                )
                selected = self._selected_path(report_url)
                self.assertEqual(selected.name, "runner_report.html")
                self.assertIn(f"{stack} current failure", selected.read_text(encoding="utf-8"))

    def test_internal_summary_metadata_is_never_selected_as_report(self):
        with tempfile.TemporaryDirectory(prefix="report-summary-") as tmp:
            started_at = time.time()
            ScriptRunnerService._write_behave_execution_summary(
                tmp,
                "1 feature passed, 0 failed, 1 skipped\n"
                "1 scenario passed, 0 failed, 19 skipped\n"
                "4 steps passed, 0 failed, 110 skipped, 0 undefined",
            )
            report_url = ScriptRunnerService._current_report_url(tmp, started_at, False, "failure")
            self.assertEqual(self._selected_path(report_url).name, "runner_report.html")


if __name__ == "__main__":
    unittest.main()
