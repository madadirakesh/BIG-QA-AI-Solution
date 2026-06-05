from selenium.webdriver.common.by import By
from pages.base_page import BasePage


class LoginPage(BasePage):
    """Page Object for the Login page."""

    # Locators
    USERNAME_INPUT = (By.ID, "txtUserID")
    PASSWORD_INPUT = (By.ID, "txtPassword")
    LOGIN_BUTTON   = (By.ID, "sub")
    ERROR_MESSAGE  = (By.CSS_SELECTOR, "[data-test='error']")

    def enter_username(self, username: str):
        self.type_text(*self.USERNAME_INPUT, username)

    def enter_password(self, password: str):
        self.type_text(*self.PASSWORD_INPUT, password)

    def click_login(self):
        self.click(*self.LOGIN_BUTTON)

    def get_error_message(self) -> str:
        return self.find(*self.ERROR_MESSAGE).text
