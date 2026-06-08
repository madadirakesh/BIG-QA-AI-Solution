import os
import base64
from datetime import datetime
from dotenv import load_dotenv
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.edge.service import Service as EdgeService
from webdriver_manager.chrome import ChromeDriverManager
from webdriver_manager.firefox import GeckoDriverManager
from webdriver_manager.microsoft import EdgeChromiumDriverManager

load_dotenv()


def before_all(context):
    """Global setup: runs once before the entire test suite."""
    os.makedirs("Results/screenshots", exist_ok=True)
    context.base_url = os.getenv("APP_URL", "https://www.saucedemo.com")
    context.headless = os.getenv("HEADLESS", "false").lower() == "true"
    context.browser_name = os.getenv("BROWSER", "chrome").lower()


def before_scenario(context, scenario):
    """Spin up a fresh browser instance before each scenario."""
    context.driver = _create_driver(context.browser_name, context.headless)
    context.driver.implicitly_wait(10)
    context.driver.maximize_window()


def after_scenario(context, scenario):
    """Capture screenshot on failure, then tear down the driver."""
    if scenario.status == "failed":
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = scenario.name.replace(" ", "_").replace("/", "-")
        screenshot_path = f"Results/screenshots/{safe_name}_{timestamp}.png"
        context.driver.save_screenshot(screenshot_path)
        # context.embed only exists when an embed-capable formatter (e.g. the HTML formatter) is
        # active; with the plain/default formatter it is absent, so guard to avoid a hook crash.
        if hasattr(context, "embed"):
            with open(screenshot_path, "rb") as img_file:
                context.embed(
                    mime_type="image/png",
                    data=base64.b64encode(img_file.read()).decode("utf-8"),
                    caption=f"Failure screenshot: {scenario.name}",
                )
    context.driver.quit()


def after_all(context):
    """Global teardown: runs once after the entire test suite."""
    pass


def _create_driver(browser_name: str, headless: bool):
    """Factory function to create the appropriate WebDriver instance."""
    if browser_name == "firefox":
        options = webdriver.FirefoxOptions()
        if headless:
            options.add_argument("--headless")
        return webdriver.Firefox(
            service=FirefoxService(GeckoDriverManager().install()),
            options=options,
        )
    elif browser_name == "edge":
        options = webdriver.EdgeOptions()
        if headless:
            options.add_argument("--headless")
        return webdriver.Edge(
            service=EdgeService(EdgeChromiumDriverManager().install()),
            options=options,
        )
    else:  # Default: Chrome
        options = webdriver.ChromeOptions()
        if headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--ignore-certificate-errors")
        return webdriver.Chrome(
            service=ChromeService(ChromeDriverManager().install()),
            options=options,
        )
