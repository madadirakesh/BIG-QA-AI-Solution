from selenium.webdriver.common.by import By
from pages.base_page import BasePage


class LoginPage(BasePage):
    """Page Object for the Login page."""

    USERNAME_INPUT = (
        By.CSS_SELECTOR,
        "#txtUserID, input[autocomplete='username'], input[placeholder*='username' i], "
        "input[placeholder*='email' i], input[type='email'], input[name*='user' i], "
        "input[id*='user' i], input[type='text']",
    )
    PASSWORD_INPUT = (
        By.CSS_SELECTOR,
        "#txtPassword, input[autocomplete='current-password'], input[placeholder*='password' i], input[type='password']",
    )
    LOGIN_BUTTON = (
        By.CSS_SELECTOR,
        "#sub, button[type='submit'], input[type='submit'], button[id*='login' i], button[name*='login' i]",
    )

    def enter_username(self, username: str):
        self.type_text_if_present(*self.USERNAME_INPUT, username)

    def enter_password(self, password: str):
        self.type_text_if_present(*self.PASSWORD_INPUT, password)

    def click_login(self):
        self.click_if_present(*self.LOGIN_BUTTON)
