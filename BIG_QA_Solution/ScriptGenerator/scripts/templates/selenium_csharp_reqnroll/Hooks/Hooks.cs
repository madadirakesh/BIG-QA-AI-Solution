using System.Text.RegularExpressions;
using OpenQA.Selenium;
using Reqnroll;
using Reqnroll.BoDi;
using SeleniumReqnrollTests.Utils;

namespace SeleniumReqnrollTests.Hooks;

/// <summary>
/// Reqnroll lifecycle hooks that wrap a fresh WebDriver around each scenario — the C# counterpart
/// of the Java template's Hooks.
///
/// Wiring:
///   - [Binding] registers this class with Reqnroll's runtime.
///   - The driver created in [BeforeScenario] is registered into the scenario's IObjectContainer
///     (Reqnroll's built-in BoDi container), so step classes receive it by simply declaring an
///     IWebDriver constructor parameter (see StepDefinitions/LoginSteps.cs). This is the idiomatic
///     Reqnroll/SpecFlow "context injection" pattern and avoids static driver state.
///
/// Failure capture: on a failed scenario we save a timestamped screenshot under
/// Results/screenshots/. Disk archival is best-effort — a failure to write it must not mask the
/// real test failure.
/// </summary>
[Binding]
public class Hooks
{
    private readonly IObjectContainer _container;
    private IWebDriver? _driver;

    // Reqnroll injects the scenario-scoped container into this constructor.
    public Hooks(IObjectContainer container)
    {
        _container = container;
    }

    [BeforeScenario]
    public void BeforeScenario()
    {
        // Fresh driver per scenario so cookies / storage / window state never leak between tests.
        var driver = DriverFactory.StartDriver();
        _driver = driver;
        // Make the driver injectable into step-definition constructors.
        _container.RegisterInstanceAs(driver);
    }

    [AfterScenario]
    public void AfterScenario(ScenarioContext scenario)
    {
        if (_driver is null) return;
        try
        {
            if (scenario.TestError is not null && _driver is ITakesScreenshot screenshotDriver)
            {
                var screenshot = screenshotDriver.GetScreenshot();
                var safeName = Regex.Replace(scenario.ScenarioInfo.Title, "[^a-zA-Z0-9-_]", "_");
                var timestamp = DateTime.Now.ToString("yyyyMMdd_HHmmss");
                var directory = Path.Combine("Results", "screenshots");
                Directory.CreateDirectory(directory);
                screenshot.SaveAsFile(Path.Combine(directory, $"{safeName}_{timestamp}.png"));
            }
        }
        catch
        {
            // Best-effort disk archival; ignore failures here.
        }
        finally
        {
            _driver.Quit();
            _driver.Dispose();
            _driver = null;
        }
    }
}
