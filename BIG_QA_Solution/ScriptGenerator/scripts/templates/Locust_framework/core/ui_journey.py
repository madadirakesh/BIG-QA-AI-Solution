"""
ui_journey.py
-------------
Selenium support for Functional (browser) journeys run by Locust.

A recorded Functional journey is an ordinary Locust script: each virtual user
opens its own Chrome, and every recorded action runs inside `ui.step(label)`,
which waits for its element (no fixed sleeps) and reports the step to Locust as
one request of type "UI". The Locust report, the HTML dashboard and the
response-time thresholds therefore treat a user action exactly like an HTTP
call - the time a real user waits for it.

When a step fails, the rest of that pass cannot run (a failed login leaves no
page to act on), but each remaining step is still reported - as a failure
"Skipped: step ... failed" - so the report always lists every recorded action.

Also usable from pytest: the methods match the `ui` fixture of the older
functional/conftest.py, so a pytest journey can be driven by Locust too.

Environment:
  PERF_UI_HEADED=1         show the browser (the Script Check sets this)
  PERF_UI_WAIT_SECONDS     how long a step waits for its element (default 15)
  PERF_RECORDED_PASSWORD   value typed into recorded password fields (_2, _3, ...
                           for further distinct passwords); set by the
                           Performance Test page from the recording
"""

import os
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import Select, WebDriverWait

LOADER_VERSION = 3
REQUEST_TYPE = "UI"
WAIT_SECONDS = float(os.getenv("PERF_UI_WAIT_SECONDS", "15"))
SCREENSHOT_DIR = Path(__file__).resolve().parent.parent / "results" / "screenshots"

# Filled by a generated payload/threshold copy of a script: step label -> ms.
_THRESHOLDS_MS = {}
_DEFAULT_THRESHOLD_MS = 0.0


def set_thresholds(per_step_ms, default_ms=0.0):
    """Fail any step slower than its limit (ms); `default_ms` covers steps without one."""
    global _DEFAULT_THRESHOLD_MS
    _THRESHOLDS_MS.clear()
    _THRESHOLDS_MS.update(per_step_ms or {})
    _DEFAULT_THRESHOLD_MS = float(default_ms or 0.0)


def _threshold_ms(label):
    for key in (f"{REQUEST_TYPE} {label}", label):
        if key in _THRESHOLDS_MS:
            return _THRESHOLDS_MS[key]
    return _DEFAULT_THRESHOLD_MS


def headed():
    return os.getenv("PERF_UI_HEADED") == "1"


def create_driver(show_browser=None):
    options = Options()
    if not (headed() if show_browser is None else show_browser):
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-dev-shm-usage")
    return webdriver.Chrome(options=options)


class UiSession:
    """The actions a recorded journey replays, each waiting for its element."""

    def __init__(self, environment=None, base_url="", driver=None, name="journey"):
        if isinstance(environment, str) and not base_url:
            base_url = environment
            environment = None
        self.environment = environment
        self.base_url = (base_url or os.getenv("PERF_BASE_URL", "")).rstrip("/")
        self.name = name
        self.timings = []
        self._driver = driver
        self._step_depth = 0
        # The step that failed in the current pass ('' while all is well).
        # Every later step of that pass is reported as skipped.
        self._failed_step = ""
        self._skipping = False
        self._first_label = None
        self._explicit_passes = False

    @property
    def driver(self):
        # Started lazily so a failing browser launch is reported as a step.
        if self._driver is None:
            started = time.perf_counter()
            try:
                self._driver = create_driver()
            except Exception as error:
                self._report("Launch browser", started, error)
                raise
            self._report("Launch browser", started, None)
        return self._driver

    def quit(self):
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception:
                pass
            self._driver = None

    # ── reporting ───────────────────────────────────────────────────────

    def _report(self, label, started, error):
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.timings.append((label, elapsed_ms / 1000))
        if error is None:
            limit = _threshold_ms(label)
            if limit and elapsed_ms > limit:
                error = AssertionError(
                    "Response time %.0f ms exceeded the %.0f ms threshold" % (elapsed_ms, limit))
        events = getattr(self.environment, "events", None)
        if events is None and hasattr(self.environment, "request"):
            events = self.environment
        if events is None:
            try:
                from locust import events as locust_events
                events = locust_events
            except Exception:
                events = None
        if events is not None and hasattr(events, "request"):
            events.request.fire(request_type=REQUEST_TYPE, name=label, response_time=elapsed_ms,
                                response_length=0, exception=error, context={}, url=label)
        return error

    def begin_pass(self):
        """A new pass of the journey starts: run every step again, even after a failed pass."""
        self._explicit_passes = True
        self._failed_step = ""

    def _start_of_pass(self, label):
        # Without begin_pass(), the journey's first step coming round again
        # marks the next pass.
        if self._first_label is None:
            self._first_label = label
        elif label == self._first_label and not self._explicit_passes:
            self._failed_step = ""

    @contextmanager
    def step(self, label):
        if self._step_depth == 0:
            self._start_of_pass(label)
        if not self._failed_step:
            try:
                driver = self.driver
            except Exception:
                # Reported as "Launch browser"; every step of this pass is skipped.
                self._failed_step = "Launch browser"
            else:
                # Every step starts from the top-level document; find() switches
                # into an iframe only when the element lives there.
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass

        self._step_depth += 1
        started = time.perf_counter()
        if self._failed_step:
            # An earlier step of this pass failed, so this one cannot run (its
            # actions are no-ops). It is still reported, as a failure, so the
            # results list every recorded action rather than stopping at the
            # first failure.
            skipped_after = self._failed_step
            was_skipping, self._skipping = self._skipping, True
            try:
                yield
            finally:
                self._skipping = was_skipping
                self._step_depth = max(0, self._step_depth - 1)
            self._report(label, started, StepSkipped(f"Skipped: step '{skipped_after}' failed earlier in this pass"))
            return

        try:
            yield
        except Exception as error:
            if _is_locust_control(error):
                raise
            self._report(label, started, error)
            self._screenshot(label)
            self._failed_step = label
        else:
            self._report(label, started, None)
        finally:
            self._step_depth = max(0, self._step_depth - 1)

    def _act(self, label, run):
        """Run an action inside the current step, or as a step of its own when called outside one."""
        if self._step_depth == 0:
            with self.step(label):
                self._act(label, run)
        elif not self._skipping:
            run()

    # ── actions ─────────────────────────────────────────────────────────

    def url(self, target):
        if target.lower().startswith(("http://", "https://")):
            return target
        return urljoin(self.base_url + "/", target.lstrip("/")) if self.base_url else target

    def open(self, target):
        self._act(f"Open {target}", lambda: self.driver.get(self.url(target)))

    def wait_for_url(self, fragment):
        """Wait for a navigation an earlier action triggered. Soft: redirects may land elsewhere."""
        def _wait():
            try:
                WebDriverWait(self.driver, WAIT_SECONDS).until(lambda d: fragment in d.current_url)
            except TimeoutException:
                pass

        self._act(f"Wait for URL containing '{fragment}'", _wait)

    def find(self, by, value, condition=EC.visibility_of_element_located):
        """Wait for the element in the page or any of its iframes."""
        expected = condition((by, value))

        def check(driver):
            try:
                return expected(driver)
            except (NoSuchElementException, StaleElementReferenceException):
                return False

        def locate(driver):
            driver.switch_to.default_content()
            found = check(driver)
            if found:
                return found
            for frame in driver.find_elements(By.TAG_NAME, "iframe"):
                try:
                    driver.switch_to.frame(frame)
                except Exception:
                    continue
                found = check(driver)
                if found:
                    return found
                driver.switch_to.default_content()
            return False

        return WebDriverWait(self.driver, WAIT_SECONDS).until(locate, f"{by}={value} was not found")

    def _click(self, element):
        try:
            element.click()
        except ElementClickInterceptedException:
            # An overlay (cookie banner, sticky header) is over the element.
            self.driver.execute_script("arguments[0].click();", element)

    def click(self, by, value):
        self._act(f"Click ({by}={value})",
                  lambda: self._click(self.find(by, value, EC.element_to_be_clickable)))

    def set_checked(self, by, value, checked):
        def _set():
            element = self.find(by, value, EC.presence_of_element_located)
            if element.is_selected() != bool(checked):
                self._click(element)

        self._act(f"Set {'checked' if checked else 'unchecked'} ({by}={value})", _set)

    def type(self, by, value, text):
        is_secret = any(term in str(value).lower() for term in ("pass", "pwd", "secret", "token"))
        val_repr = "'***'" if is_secret else (repr(text) if len(str(text)) <= 30 else repr(str(text)[:27] + "..."))

        def _type():
            element = self.find(by, value)
            element.clear()
            element.send_keys("" if text is None else str(text))

        self._act(f"Type {val_repr} into ({by}={value})", _type)

    def select(self, by, value, option_text):
        self._act(f"Select '{option_text}' in ({by}={value})",
                  lambda: Select(self.find(by, value, EC.presence_of_element_located))
                  .select_by_visible_text(str(option_text)))

    def press_enter(self, by, value):
        self._act(f"Press ENTER on ({by}={value})", lambda: self.find(by, value).send_keys(Keys.ENTER))

    def submit(self, by, value):
        self._act(f"Submit ({by}={value})",
                  lambda: self.find(by, value, EC.presence_of_element_located).submit())

    def _screenshot(self, label):
        try:
            SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe = "".join(ch if ch.isalnum() else "_" for ch in label)[:60]
            self.driver.save_screenshot(str(SCREENSHOT_DIR / f"{self.name}_{stamp}_{safe}.png"))
        except Exception:
            pass


class StepSkipped(Exception):
    """Reported for a step that could not run because an earlier step of its pass failed."""


def _is_locust_control(error):
    try:
        from locust.exception import InterruptTaskSet, RescheduleTask, StopUser
    except Exception:
        return False
    return isinstance(error, (InterruptTaskSet, RescheduleTask, StopUser))
