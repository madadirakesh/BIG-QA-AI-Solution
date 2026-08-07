package pageObjects;

import com.microsoft.playwright.Locator;
import com.microsoft.playwright.Page;
import com.microsoft.playwright.options.WaitUntilState;

/**
 * Page Object for the application's login screen, used by the shipped sample
 * (src/test/resources/features/loginFeature.feature + stepDefinitions/LoginSteps.java).
 *
 * Why this exists:
 *   The scaffold needs to run green on the very first `mvn test`, BEFORE the Script Developer
 *   (AI) wizard generates real, app-specific page objects. Two competing goals shape the design:
 *
 *     1. Demonstrate the intended Page Object Model so a developer sees where real locators and
 *        actions belong (this class is the example they will copy).
 *     2. Pass against ANY {{BASE_URL}} the user typed into the creation modal — we cannot know
 *        the target app's real markup at scaffold time.
 *
 *   To satisfy both, every interaction below is "best effort": we only touch an element if it is
 *   actually present, and the final verification only asserts the page loaded (not an app
 *   specific element). Replace these locators and tighten isLoaded() with real expectations the
 *   moment you start writing genuine tests — that is the whole point of the wizard.
 */
public class LoginPage {

    private final Page page;

    // Each selector lists the team's standard-app locator first (the same element IDs the
    // Selenium template targets: txtUserID / txtPassword / sub), then generic CSS fallbacks so
    // the sample still finds a login form on an unknown app. Comma-separated selectors are
    // Playwright "or" groups — the first match wins. Swap these for your app's real, unique
    // locators when you build real tests.
    private static final String USERNAME_SELECTOR =
            "#txtUserID, input[autocomplete='username'], input[placeholder*='username' i], input[placeholder*='email' i], input[type='email'], input[name*='user' i], input[id*='user' i], input[type='text']";
    private static final String PASSWORD_SELECTOR =
            "#txtPassword, input[autocomplete='current-password'], input[placeholder*='password' i], input[type='password']";
    private static final String LOGIN_BUTTON_SELECTOR =
            "#sub, button[type='submit'], input[type='submit'], button:has-text('Login'), button:has-text('Sign in')";

    public LoginPage(Page page) {
        this.page = page;
    }

    /**
     * Navigates to the application's base URL. We wait for DOMCONTENTLOADED rather than the full
     * "load" event so the step does not hang on slow third-party resources (analytics, fonts);
     * the DOM being ready is enough to interact with a login form.
     */
    public void open(String appUrl) {
        page.navigate(appUrl, new Page.NavigateOptions().setWaitUntil(WaitUntilState.DOMCONTENTLOADED));
    }

    /**
     * Fills the username and password fields when they are present. See fillIfPresent() for why
     * the presence guard matters — without it the sample would hang for the full Playwright
     * timeout (and then fail) on any page that has no login form.
     */
    public void login(String username, String password) {
        fillIfPresent(USERNAME_SELECTOR, username);
        fillIfPresent(PASSWORD_SELECTOR, password);
    }

    /**
     * Clicks the login / submit button if one is present, then waits for any resulting
     * navigation to settle. No-ops when the page has no recognisable submit control.
     */
    public void submit() {
        Locator button = page.locator(LOGIN_BUTTON_SELECTOR);
        if (button.count() > 0) {
            try {
                button.first().click();
                page.waitForLoadState();
            } catch (RuntimeException ignored) {
                // Best-effort: a click that cannot complete (overlay, disabled control on an
                // unknown app) must not fail the shipped smoke sample. Real tests should let
                // such failures surface — remove this catch when you add real assertions.
            }
        }
    }

    /**
     * The smoke-level "redirected to homepage" check. We deliberately assert only that the page
     * actually loaded — it has a resolved URL and a <body> — instead of looking for an
     * app-specific post-login element, so the shipped sample passes against any reachable
     * {{BASE_URL}}. Tighten this (e.g. assert a dashboard element is visible, or that the login
     * button is gone) when you write real tests.
     */
    public boolean isLoaded() {
        return !page.url().isBlank() && page.locator("body").count() > 0;
    }

    /**
     * Fills {@code selector} with {@code value} only if at least one matching element exists.
     * locator(...).count() returns the current match count WITHOUT Playwright's auto-wait, so the
     * guard is instant — unlike fill(), which would auto-wait up to the default timeout and then
     * throw if the element never appears. The try/catch covers the present-but-not-editable case
     * (hidden/readonly field on an unknown app) for the same green-by-default reason.
     */
    private void fillIfPresent(String selector, String value) {
        Locator field = page.locator(selector);
        if (field.count() > 0) {
            try {
                field.first().fill(value);
            } catch (RuntimeException ignored) {
                // See submit(): best-effort interaction for the sample only.
            }
        }
    }
}
