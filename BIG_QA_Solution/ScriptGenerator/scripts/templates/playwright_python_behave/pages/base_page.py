from playwright.sync_api import Page


class BasePage:
    """Base Page Object — shared Playwright helpers for all page classes.

    Mirrors the Selenium template's BasePage (same method names: open / click / type_text /
    is_visible) so step definitions read identically across the Selenium and Playwright Python
    templates. Locators are plain CSS/text selector strings passed straight to Playwright.
    """

    DEFAULT_TIMEOUT = 15000  # milliseconds — Playwright timeouts are in ms, unlike Selenium's seconds

    def __init__(self, page: Page):
        self.page = page

    def open(self, url: str):
        self.page.goto(url)

    def click(self, selector: str):
        self.page.locator(selector).first.click(timeout=self.DEFAULT_TIMEOUT)

    def type_text(self, selector: str, text: str):
        self.page.locator(selector).first.fill(text, timeout=self.DEFAULT_TIMEOUT)

    def is_visible(self, selector: str) -> bool:
        try:
            return self.page.locator(selector).first.is_visible()
        except Exception:
            return False

    def count(self, selector: str) -> int:
        """Number of elements matching the selector (0 if none) — handy for lenient checks."""
        return self.page.locator(selector).count()
