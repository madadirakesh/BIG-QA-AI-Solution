"""
conftest.py
-----------
Shared Selenium fixtures for the functional journeys recorded from the
Performance Test page (functional/test_*.py).

Every recorded action runs inside `ui.step(...)`, which waits for its element
(no fixed sleeps), times the step, and screenshots the page if the step fails.
The timings are printed after each test - run with `-s` to see them - so a
functional journey doubles as a measure of what a real user waits for.

Options / environment:
  --headed                 show the browser (default: headless Chrome)
  PERF_UI_HEADED=1         same as --headed
  PERF_BASE_URL            point the journey at another environment
  PERF_UI_WAIT_SECONDS     how long a step waits for its element (default 15)
  PERF_RECORDED_PASSWORD   value typed into recorded password fields
"""

import os
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pytest
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

WAIT_SECONDS = float(os.getenv("PERF_UI_WAIT_SECONDS", "15"))
RESULTS_DIR = Path(__file__).resolve().parent.parent / "results" / "functional"


def pytest_addoption(parser):
    parser.addoption("--headed", action="store_true", default=False,
                     help="Show the browser while the recorded journey runs.")


@pytest.fixture
def driver(request):
    options = Options()
    if not (request.config.getoption("--headed") or os.getenv("PERF_UI_HEADED") == "1"):
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1920,1080")
    browser = webdriver.Chrome(options=options)
    yield browser
    browser.quit()


class UiSteps:
    """The actions a recorded journey replays, each waiting for its element."""

    def __init__(self, driver, test_name):
        self.driver = driver
        self.test_name = test_name
        self.timings = []

    @contextmanager
    def step(self, label):
        # Every step starts from the top-level document; find() switches into
        # an iframe only when the element lives there.
        self.driver.switch_to.default_content()
        started = time.perf_counter()
        try:
            yield
        except Exception:
            self._screenshot(label)
            raise
        finally:
            self.timings.append((label, time.perf_counter() - started))

    def open(self, url):
        self.driver.get(url)

    def wait_for_url(self, fragment):
        """Wait for a navigation an earlier action triggered. Soft: redirects may land elsewhere."""
        try:
            WebDriverWait(self.driver, WAIT_SECONDS).until(lambda d: fragment in d.current_url)
        except TimeoutException:
            pass

    def find(self, by, value, condition=EC.visibility_of_element_located):
        """
        Wait for the element in the page or any of its iframes (the recorder
        captures actions inside frames too). Found in a frame, the driver stays
        switched into it for the action; the next step starts from the top again.
        """
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
        self._click(self.find(by, value, EC.element_to_be_clickable))

    def set_checked(self, by, value, checked):
        # Styled checkboxes are often visually hidden, so wait for presence only.
        element = self.find(by, value, EC.presence_of_element_located)
        if element.is_selected() != checked:
            self._click(element)

    def type(self, by, value, text):
        element = self.find(by, value)
        element.clear()
        element.send_keys(text)

    def select(self, by, value, option_text):
        Select(self.find(by, value, EC.presence_of_element_located)).select_by_visible_text(option_text)

    def press_enter(self, by, value):
        self.find(by, value).send_keys(Keys.ENTER)

    def submit(self, by, value):
        self.find(by, value, EC.presence_of_element_located).submit()

    def _screenshot(self, label):
        try:
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe = "".join(ch if ch.isalnum() else "_" for ch in label)[:60]
            self.driver.save_screenshot(str(RESULTS_DIR / f"{self.test_name}_{stamp}_{safe}.png"))
        except Exception:
            pass


@pytest.fixture
def ui(driver, request):
    steps = UiSteps(driver, request.node.name)
    yield steps
    total = sum(seconds for _, seconds in steps.timings)
    print(f"\n{request.node.name}: {len(steps.timings)} step(s) in {total:.2f}s")
    for label, seconds in steps.timings:
        print(f"  {seconds:7.2f}s  {label}")
    request.node.user_properties.append(("step_timings", steps.timings))
