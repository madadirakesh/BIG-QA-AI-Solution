package utils;

import com.microsoft.playwright.Browser;
import com.microsoft.playwright.BrowserContext;
import com.microsoft.playwright.BrowserType;
import com.microsoft.playwright.Page;
import com.microsoft.playwright.Playwright;

/**
 * Thread-local Playwright lifecycle owner.
 *
 * Why ThreadLocal: Cucumber + JUnit Platform can run scenarios in parallel (set via
 * cucumber.execution.parallel.enabled in junit-platform.properties). Each thread that runs a
 * scenario needs its own Playwright, Browser, Context and Page — sharing a Page across threads
 * causes Playwright to throw. Storing them per-thread keeps the design parallel-safe without
 * forcing the rest of the project to plumb Page through every method signature.
 *
 * Lifecycle: Hooks.beforeScenario() calls startBrowser() — step definitions call getPage() —
 * Hooks.afterScenario() calls closeBrowser(). Do not call startBrowser() outside the @Before
 * hook or you will leak resources.
 */
public final class PlaywrightFactory {

    private static final ThreadLocal<Playwright> PLAYWRIGHT = new ThreadLocal<>();
    private static final ThreadLocal<Browser> BROWSER = new ThreadLocal<>();
    private static final ThreadLocal<BrowserContext> CONTEXT = new ThreadLocal<>();
    private static final ThreadLocal<Page> PAGE = new ThreadLocal<>();

    private PlaywrightFactory() {
        // utility class — not meant to be instantiated.
    }

    /**
     * Starts Playwright, launches the configured browser, and opens a fresh page.
     * Returns the Page so callers can keep a local reference if convenient, but most code
     * should call {@link #getPage()} from inside step definitions.
     */
    public static Page startBrowser() {
        Playwright playwright = Playwright.create();
        PLAYWRIGHT.set(playwright);

        String browserName = ConfigReader.getOrDefault("BROWSER", "chromium").toLowerCase();
        boolean headless = Boolean.parseBoolean(ConfigReader.getOrDefault("HEADLESS", "false"));

        BrowserType.LaunchOptions launchOptions = new BrowserType.LaunchOptions().setHeadless(headless);
        Browser browser;
        switch (browserName) {
            case "firefox":
                browser = playwright.firefox().launch(launchOptions);
                break;
            case "webkit":
                browser = playwright.webkit().launch(launchOptions);
                break;
            default:
                // Fall through to chromium for any unknown value rather than fail —
                // this keeps CI smoke runs forgiving.
                browser = playwright.chromium().launch(launchOptions);
        }
        BROWSER.set(browser);

        // ignoreHTTPSErrors=true matches the TypeScript template hooks. Useful when QA points at
        // an internal staging environment with a self-signed cert. Tighten for production runs.
        BrowserContext context = browser.newContext(
                new Browser.NewContextOptions().setIgnoreHTTPSErrors(true)
        );
        CONTEXT.set(context);

        Page page = context.newPage();
        PAGE.set(page);
        return page;
    }

    /** Returns the current scenario's Page. Call only between @Before and @After. */
    public static Page getPage() {
        return PAGE.get();
    }

    /**
     * Closes everything for the current thread and clears the ThreadLocals.
     * Closures are independent so a failure to close one stage does not prevent the rest.
     */
    public static void closeBrowser() {
        try { if (PAGE.get() != null) PAGE.get().close(); } finally { PAGE.remove(); }
        try { if (CONTEXT.get() != null) CONTEXT.get().close(); } finally { CONTEXT.remove(); }
        try { if (BROWSER.get() != null) BROWSER.get().close(); } finally { BROWSER.remove(); }
        try { if (PLAYWRIGHT.get() != null) PLAYWRIGHT.get().close(); } finally { PLAYWRIGHT.remove(); }
    }
}
