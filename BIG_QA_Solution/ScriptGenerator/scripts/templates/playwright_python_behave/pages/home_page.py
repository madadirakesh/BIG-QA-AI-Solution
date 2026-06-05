from pages.base_page import BasePage


class HomePage(BasePage):
    """Page Object for the Home / landing page (Playwright)."""

    def is_loaded(self) -> bool:
        # Lenient "homepage" check: the page navigated somewhere and has a body. Tighten this
        # (e.g. assert a real heading/title) when you write genuine tests.
        return bool(self.page.url) and self.count("body") > 0
