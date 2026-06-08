using OpenQA.Selenium;

namespace SeleniumReqnrollTests.PageObjects;

/// <summary>
/// Page Object for the login screen used by the shipped sample — the C# counterpart of the Java
/// template's LoginPage. Interactions are best-effort and the verification only checks that the
/// page loaded, so `dotnet test` passes against any {{BASE_URL}} before real tests are written.
/// Replace the locators and tighten IsLoaded() when you build genuine tests.
/// </summary>
public class LoginPage
{
    private readonly IWebDriver _driver;

    // Team standard-app locators first (txtUserID / txtPassword / sub), then generic fallbacks.
    private static readonly By Username = By.CssSelector(
        "#txtUserID, input[type='email'], input[name*='user' i], input[id*='user' i], input[type='text']");
    private static readonly By Password = By.CssSelector("#txtPassword, input[type='password']");
    private static readonly By LoginButton = By.CssSelector(
        "#sub, button[type='submit'], input[type='submit'], button[id*='login' i], button[name*='login' i]");

    public LoginPage(IWebDriver driver)
    {
        _driver = driver;
    }

    public void Open(string appUrl) => _driver.Navigate().GoToUrl(appUrl);

    public void Login(string username, string password)
    {
        TypeIfPresent(Username, username);
        TypeIfPresent(Password, password);
    }

    public void Submit()
    {
        var buttons = Probe(LoginButton);
        if (buttons.Count > 0)
        {
            try
            {
                buttons[0].Click();
            }
            catch
            {
                // Best-effort: a click that can't complete on an unknown app must not fail the sample.
            }
        }
    }

    /// <summary>Lenient "homepage" check: the page has a resolved URL and a body. Tighten for real tests.</summary>
    public bool IsLoaded()
    {
        var url = _driver.Url;
        return !string.IsNullOrWhiteSpace(url) && Probe(By.TagName("body")).Count > 0;
    }

    private void TypeIfPresent(By locator, string value)
    {
        var elements = Probe(locator);
        if (elements.Count > 0)
        {
            try
            {
                elements[0].Clear();
                elements[0].SendKeys(value);
            }
            catch
            {
                // Present-but-not-editable on an unknown app — ignore for the sample only.
            }
        }
    }

    /// <summary>
    /// FindElements with the implicit wait temporarily set to zero, so a missing element returns an
    /// empty collection instantly instead of blocking for the full 10s implicit wait.
    /// </summary>
    private IReadOnlyList<IWebElement> Probe(By locator)
    {
        _driver.Manage().Timeouts().ImplicitWait = TimeSpan.Zero;
        try
        {
            return _driver.FindElements(locator);
        }
        finally
        {
            _driver.Manage().Timeouts().ImplicitWait = TimeSpan.FromSeconds(10);
        }
    }
}
