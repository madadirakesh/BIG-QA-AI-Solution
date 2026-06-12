import os
import base64
from datetime import datetime
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright


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
    """Global setup: start Playwright once and launch the browser for the whole suite."""
    os.makedirs("Results/screenshots", exist_ok=True)
    context.base_url = os.getenv("APP_URL", "https://www.saucedemo.com")
    context.headless = os.getenv("HEADLESS", "false").lower() == "true"
    context.browser_name = os.getenv("BROWSER", "chromium").lower()
    context.playwright = sync_playwright().start()
    # Unknown browser names fall back to Chromium rather than failing — keeps smoke runs forgiving.
    browser_type = getattr(context.playwright, context.browser_name, context.playwright.chromium)
    context.browser = browser_type.launch(headless=context.headless)


def before_scenario(context, scenario):
    """Fresh browser context + page per scenario so cookies/storage never leak between tests."""
    # ignore_https_errors mirrors the other templates' choice — handy for internal staging certs.
    context.context = context.browser.new_context(ignore_https_errors=True)
    context.page = context.context.new_page()


def after_scenario(context, scenario):
    """Capture a screenshot on failure (embedded in the report + saved to disk), then tear down."""
    if scenario.status == "failed" and getattr(context, "page", None):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = scenario.name.replace(" ", "_").replace("/", "-")
        screenshot_path = f"Results/screenshots/{safe_name}_{timestamp}.png"
        context.page.screenshot(path=screenshot_path, full_page=True)
        # context.embed only exists when an embed-capable formatter (e.g. the HTML formatter) is
        # active; with the plain/default formatter it is absent, so guard to avoid a hook crash.
        if hasattr(context, "embed"):
            with open(screenshot_path, "rb") as img_file:
                context.embed(
                    mime_type="image/png",
                    data=base64.b64encode(img_file.read()).decode("utf-8"),
                    caption=f"Failure screenshot: {scenario.name}",
                )
    if getattr(context, "page", None):
        context.page.close()
    if getattr(context, "context", None):
        context.context.close()


def after_all(context):
    """Global teardown: close the browser and stop Playwright."""
    if getattr(context, "browser", None):
        context.browser.close()
    if getattr(context, "playwright", None):
        context.playwright.stop()
