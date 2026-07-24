package pageObjects;

import org.openqa.selenium.By;
import org.openqa.selenium.WebDriver;
import org.openqa.selenium.WebElement;

import java.time.Duration;
import java.util.List;

/**
 * Page Object for the login screen used by the shipped sample (Selenium counterpart of the
 * Playwright template's LoginPage). Interactions are best-effort and the verification only checks
 * the page loaded, so `mvn test` passes against any {{BASE_URL}} before real tests are written.
 * Replace the locators and tighten isLoaded() when you build genuine tests.
 */
public class LoginPage {

    private final WebDriver driver;

    // Team standard-app locators first (txtUserID / txtPassword / sub), then generic fallbacks.
    private static final By USERNAME = By.cssSelector(
            "#txtUserID, input[autocomplete='username'], input[placeholder*='username' i], input[placeholder*='email' i], input[type='email'], input[name*='user' i], input[id*='user' i], input[type='text']");
    private static final By PASSWORD = By.cssSelector("#txtPassword, input[autocomplete='current-password'], input[placeholder*='password' i], input[type='password']");
    private static final By LOGIN_BUTTON = By.cssSelector(
            "#sub, button[type='submit'], input[type='submit'], button[id*='login' i], button[name*='login' i]");

    public LoginPage(WebDriver driver) {
        this.driver = driver;
    }

    public void open(String appUrl) {
        driver.get(appUrl);
    }

    public void login(String username, String password) {
        typeIfPresent(USERNAME, username);
        typeIfPresent(PASSWORD, password);
    }

    public void submit() {
        List<WebElement> button = probe(LOGIN_BUTTON);
        if (!button.isEmpty()) {
            try {
                button.get(0).click();
            } catch (RuntimeException ignored) {
                // Best-effort: a click that can't complete on an unknown app must not fail the sample.
            }
        }
    }

    /** Lenient "homepage" check: the page has a resolved URL and a body. Tighten for real tests. */
    public boolean isLoaded() {
        String url = driver.getCurrentUrl();
        return url != null && !url.isBlank() && !probe(By.tagName("body")).isEmpty();
    }

    private void typeIfPresent(By locator, String value) {
        List<WebElement> els = probe(locator);
        if (!els.isEmpty()) {
            try {
                els.get(0).clear();
                els.get(0).sendKeys(value);
            } catch (RuntimeException ignored) {
                // Present-but-not-editable on an unknown app — ignore for the sample only.
            }
        }
    }

    /**
     * findElements with implicit wait temporarily set to zero, so a missing element returns an
     * empty list instantly instead of blocking for the full 10s implicit wait.
     */
    private List<WebElement> probe(By locator) {
        driver.manage().timeouts().implicitlyWait(Duration.ZERO);
        try {
            return driver.findElements(locator);
        } finally {
            driver.manage().timeouts().implicitlyWait(Duration.ofSeconds(10));
        }
    }
}
