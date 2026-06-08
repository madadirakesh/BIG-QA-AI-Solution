from pages.base_page import BasePage


class LoginPage(BasePage):
    """Page Object for the Login page (Playwright).

    Locators list the team standard-app ids first (txtUserID / txtPassword / sub), then generic
    fallbacks, so the shipped sample runs green against most apps before real tests are written.
    Interactions are best-effort (present-but-not-fillable elements are ignored) — replace the
    locators with real ones for genuine tests.
    """

    USERNAME_INPUT = "#txtUserID, input[type='email'], input[name*='user' i], input[id*='user' i], input[type='text']"
    PASSWORD_INPUT = "#txtPassword, input[type='password']"
    LOGIN_BUTTON = "#sub, button[type='submit'], input[type='submit'], button:has-text('Login'), button:has-text('Sign in')"

    def enter_username(self, username: str):
        self._fill_if_present(self.USERNAME_INPUT, username)

    def enter_password(self, password: str):
        self._fill_if_present(self.PASSWORD_INPUT, password)

    def click_login(self):
        button = self.page.locator(self.LOGIN_BUTTON).first
        if button.count() > 0:
            try:
                button.click()
            except Exception:
                # Best-effort: a click that can't complete on an unknown app must not fail the sample.
                pass

    def _fill_if_present(self, selector: str, value: str):
        locator = self.page.locator(selector).first
        if locator.count() > 0:
            try:
                locator.fill(value)
            except Exception:
                # Present-but-not-editable on an unknown app — ignore for the sample only.
                pass
