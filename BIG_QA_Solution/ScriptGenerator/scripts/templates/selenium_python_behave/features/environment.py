import os
import base64
from datetime import datetime
from dotenv import load_dotenv
from selenium import webdriver


def _decrypt_env_password():
    """
    If PASSWORD in the loaded .env is AES-GCM encrypted ("ENC:<token>"), decrypt it in place using
    CRED_KEY so os.getenv("PASSWORD") returns plaintext everywhere (steps/pages read it directly).

    Token layout: "ENC:" + base64(nonce(12) || ciphertext || gcmTag(16)); the key is the base64
    CRED_KEY written into this project's .env at scaffold time. Values without the ENC: prefix are
    left untouched. A decrypt failure is swallowed so a wrong/missing key surfaces as a failed
    login (easy to debug) rather than crashing the test run at import time.
    """
    enc = os.environ.get("PASSWORD", "")
    key_b64 = os.environ.get("CRED_KEY")
    if not enc.startswith("ENC:") or not key_b64:
        return
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        raw = base64.b64decode(enc[4:])
        nonce, ct_and_tag = raw[:12], raw[12:]
        os.environ["PASSWORD"] = AESGCM(base64.b64decode(key_b64)).decrypt(nonce, ct_and_tag, None).decode("utf-8")
    except Exception:
        pass


load_dotenv()
_decrypt_env_password()


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
    driver = getattr(context, "driver", None)
    if driver is None:
        return

    if scenario.status == "failed":
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = scenario.name.replace(" ", "_").replace("/", "-")
        screenshot_path = f"Results/screenshots/{safe_name}_{timestamp}.png"
        try:
            driver.save_screenshot(screenshot_path)
            # context.embed only exists when an embed-capable formatter (e.g. the HTML formatter) is
            # active; with the plain/default formatter it is absent, so guard to avoid a hook crash.
            if hasattr(context, "embed"):
                with open(screenshot_path, "rb") as img_file:
                    context.embed(
                        mime_type="image/png",
                        data=base64.b64encode(img_file.read()).decode("utf-8"),
                        caption=f"Failure screenshot: {scenario.name}",
                    )
        except Exception:
            pass
    try:
        driver.quit()
    except Exception:
        pass


def after_all(context):
    """Global teardown: runs once after the entire test suite."""
    pass


def _create_driver(browser_name: str, headless: bool):
    """Factory function to create the appropriate WebDriver instance.

    Selenium 4 ships with Selenium Manager, so no separate driver-manager package is needed.
    That keeps local execution stable even when external driver downloads are flaky or blocked.
    """
    if browser_name == "firefox":
        options = webdriver.FirefoxOptions()
        if headless:
            options.add_argument("--headless")
        return webdriver.Firefox(options=options)
    elif browser_name == "edge":
        options = webdriver.EdgeOptions()
        if headless:
            options.add_argument("--headless")
        return webdriver.Edge(options=options)
    else:  # Default: Chrome
        options = webdriver.ChromeOptions()
        if headless:
            options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--ignore-certificate-errors")
        return webdriver.Chrome(options=options)
