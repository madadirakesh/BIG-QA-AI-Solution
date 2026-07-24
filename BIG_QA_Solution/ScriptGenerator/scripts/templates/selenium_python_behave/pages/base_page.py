from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.common.exceptions import TimeoutException


class BasePage:
    """Base Page Object — shared helpers for all page classes."""

    DEFAULT_TIMEOUT = 15

    def __init__(self, driver: WebDriver):
        self.driver = driver
        self.wait = WebDriverWait(driver, self.DEFAULT_TIMEOUT)

    def find(self, by: By, locator: str):
        return self.wait.until(EC.presence_of_element_located((by, locator)))

    def click(self, by: By, locator: str):
        element = self.wait.until(EC.element_to_be_clickable((by, locator)))
        element.click()

    def type_text(self, by: By, locator: str, text: str):
        element = self.find(by, locator)
        element.clear()
        element.send_keys(text)

    def find_optional(self, by: By, locator: str):
        try:
            return self.find(by, locator)
        except TimeoutException:
            return None

    def click_if_present(self, by: By, locator: str):
        try:
            self.click(by, locator)
        except TimeoutException:
            return None

    def type_text_if_present(self, by: By, locator: str, text: str):
        element = self.find_optional(by, locator)
        if element is None:
            return None
        try:
            element.clear()
            element.send_keys(text)
        except Exception:
            return None

    def is_visible(self, by: By, locator: str) -> bool:
        try:
            self.wait.until(EC.visibility_of_element_located((by, locator)))
            return True
        except Exception:
            return False
