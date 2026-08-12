from .selenium_hook import SeleniumMonitor
from .playwright_hook import PlaywrightMonitor
from .watcher import Watcher, watch
from .collector import PerformanceSession
from .report import write_json_report, write_html_report, write_reports, write_consolidated_report

__all__ = [
    "SeleniumMonitor",
    "PlaywrightMonitor",
    "Watcher",
    "watch",
    "PerformanceSession",
    "write_json_report",
    "write_html_report",
    "write_reports",
    "write_consolidated_report",
]

__version__ = "0.1.0"
