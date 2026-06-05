using OpenQA.Selenium;
using OpenQA.Selenium.Chrome;
using OpenQA.Selenium.Edge;
using OpenQA.Selenium.Firefox;

namespace SeleniumReqnrollTests.Utils;

/// <summary>
/// Owns WebDriver creation — the C# counterpart of the Java template's DriverFactory.
///
/// Reqnroll runs scenarios sequentially by default, and the driver's lifetime is scoped to a
/// single scenario (Hooks creates one in [BeforeScenario] and quits it in [AfterScenario]), so a
/// plain factory method is enough here — no ThreadLocal needed unless you enable parallel
/// execution later.
///
/// Selenium 4.6+ bundles Selenium Manager, which resolves and caches the correct browser driver
/// (chromedriver/geckodriver/edgedriver) automatically on first use — so there is no driver binary
/// to download or configure.
/// </summary>
public static class DriverFactory
{
    /// <summary>
    /// Launches the browser named by the BROWSER env var (chrome | firefox | edge; defaults to
    /// chrome) with a sensible implicit wait and a maximised window. Unknown values fall through
    /// to Chrome so a misconfigured .env never hard-fails the run.
    /// </summary>
    public static IWebDriver StartDriver()
    {
        var browser = ConfigReader.GetOrDefault("BROWSER", "chrome").ToLowerInvariant();
        var headless = bool.TryParse(ConfigReader.GetOrDefault("HEADLESS", "false"), out var h) && h;

        IWebDriver driver;
        switch (browser)
        {
            case "firefox":
                var firefoxOptions = new FirefoxOptions();
                if (headless) firefoxOptions.AddArgument("--headless");
                driver = new FirefoxDriver(firefoxOptions);
                break;
            case "edge":
                var edgeOptions = new EdgeOptions();
                if (headless) edgeOptions.AddArgument("--headless=new");
                driver = new EdgeDriver(edgeOptions);
                break;
            default:
                var chromeOptions = new ChromeOptions();
                if (headless) chromeOptions.AddArgument("--headless=new");
                // --no-sandbox + --disable-dev-shm-usage are required for Chrome in most Docker/CI
                // environments; --ignore-certificate-errors matches the Playwright template's
                // ignoreHTTPSErrors choice, handy for internal staging with self-signed certs.
                chromeOptions.AddArguments(
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--ignore-certificate-errors");
                driver = new ChromeDriver(chromeOptions);
                break;
        }

        driver.Manage().Timeouts().ImplicitWait = TimeSpan.FromSeconds(10);
        driver.Manage().Window.Maximize();
        return driver;
    }
}
