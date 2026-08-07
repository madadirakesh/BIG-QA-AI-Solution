from selenium.webdriver.common.by import By
from pages.base_page import BasePage


class HomePage(BasePage):
    """Page Object for the Home / Products page."""

    def is_loaded(self) -> bool:
        return bool(self.driver.current_url) and self.find_optional(By.TAG_NAME, "body") is not None
