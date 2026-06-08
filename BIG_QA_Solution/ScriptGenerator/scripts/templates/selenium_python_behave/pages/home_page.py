from selenium.webdriver.common.by import By
from pages.base_page import BasePage


class HomePage(BasePage):
    """Page Object for the Home / Products page."""

    # Locators
    PAGE_TITLE = (By.CSS_SELECTOR, "div.app-logo-title")

    def is_loaded(self) -> bool:
        return self.is_visible(*self.PAGE_TITLE)

    def get_title_text(self) -> str:
        return self.find(*self.PAGE_TITLE).text
