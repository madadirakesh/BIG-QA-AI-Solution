package utils;

import org.openqa.selenium.WebDriver;
import org.openqa.selenium.chrome.ChromeDriver;
import org.openqa.selenium.chrome.ChromeOptions;
import org.openqa.selenium.edge.EdgeDriver;
import org.openqa.selenium.edge.EdgeOptions;
import org.openqa.selenium.firefox.FirefoxDriver;
import org.openqa.selenium.firefox.FirefoxOptions;

import java.time.Duration;

/**
 * Thread-local WebDriver lifecycle owner — Selenium counterpart of PlaywrightFactory.
 *
 * Why ThreadLocal: Cucumber + JUnit Platform can run scenarios in parallel
 * (cucumber.execution.parallel.enabled in junit-platform.properties). Each thread needs its
 * own WebDriver; sharing a single instance across threads causes WebDriver to throw. The
 * ThreadLocal keeps the design parallel-safe without plumbing the driver through every method
 * signature.
 *
 * Lifecycle: Hooks.beforeScenario() calls startDriver() — step definitions call getDriver() —
 * Hooks.afterScenario() calls quitDriver(). Do not call startDriver() outside the @Before hook
 * or you will leak browser processes.
 *
 * Selenium Manager, built into Selenium 4, resolves and caches the right driver binary for the
 * installed browser. No separate driver-manager library or hard-coded driver path is needed.
 */
public final class DriverFactory {

    private static final ThreadLocal<WebDriver> DRIVER = new ThreadLocal<>();

    private DriverFactory() {
        // utility class — not meant to be instantiated.
    }

    /**
     * Launches the configured browser (Selenium Manager resolves its driver automatically) and
     * applies a sensible implicit wait + window maximisation. Returns the driver so callers
     * may keep a local reference, but most code should call {@link #getDriver()} from inside
     * step definitions instead of holding their own reference.
     */
    public static WebDriver startDriver() {
        String browser = ConfigReader.getOrDefault("BROWSER", "chrome").toLowerCase();
        boolean headless = Boolean.parseBoolean(ConfigReader.getOrDefault("HEADLESS", "false"));

        WebDriver driver;
        switch (browser) {
            case "firefox":
                FirefoxOptions firefoxOptions = new FirefoxOptions();
                if (headless) firefoxOptions.addArguments("--headless");
                driver = new FirefoxDriver(firefoxOptions);
                break;
            case "edge":
                EdgeOptions edgeOptions = new EdgeOptions();
                if (headless) edgeOptions.addArguments("--headless=new");
                driver = new EdgeDriver(edgeOptions);
                break;
            default:
                // Fall through to Chrome for any unknown value rather than fail —
                // keeps CI smoke runs forgiving.
                ChromeOptions chromeOptions = new ChromeOptions();
                if (headless) chromeOptions.addArguments("--headless=new");
                // --no-sandbox + --disable-dev-shm-usage are required for Chrome to run in
                // most Docker/CI environments. --ignore-certificate-errors matches the
                // Playwright template's ignoreHTTPSErrors choice, useful for internal staging.
                chromeOptions.addArguments(
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--ignore-certificate-errors"
                );
                driver = new ChromeDriver(chromeOptions);
        }

        driver.manage().timeouts().implicitlyWait(Duration.ofSeconds(10));
        driver.manage().window().maximize();
        DRIVER.set(driver);
        return driver;
    }

    /** Returns the current scenario's WebDriver. Call only between @Before and @After. */
    public static WebDriver getDriver() {
        return DRIVER.get();
    }

    /**
     * Quits the driver for the current thread and clears the ThreadLocal. Safe to call when
     * no driver has been started (it just no-ops).
     */
    public static void quitDriver() {
        try {
            if (DRIVER.get() != null) {
                DRIVER.get().quit();
            }
        } finally {
            DRIVER.remove();
        }
    }
}
