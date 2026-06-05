using NUnit.Framework;
using OpenQA.Selenium;
using Reqnroll;
using SeleniumReqnrollTests.PageObjects;
using SeleniumReqnrollTests.Utils;

namespace SeleniumReqnrollTests.StepDefinitions;

/// <summary>
/// Step definitions for LoginFeature.feature. The IWebDriver is created by Hooks and injected here
/// via Reqnroll's context injection (constructor parameter). Shipped sample that runs green out of
/// the box — replace with real steps when you write genuine tests.
/// </summary>
[Binding]
public class LoginSteps
{
    private readonly IWebDriver _driver;
    private readonly LoginPage _loginPage;

    public LoginSteps(IWebDriver driver)
    {
        _driver = driver;
        _loginPage = new LoginPage(driver);
    }

    [Given("I launch the application")]
    public void GivenILaunchTheApplication()
    {
        _loginPage.Open(ConfigReader.GetAppUrl());
    }

    [When("I enter valid Username and Password")]
    public void WhenIEnterValidUsernameAndPassword()
    {
        _loginPage.Login(
            ConfigReader.GetOrDefault("USER", "standard_user"),
            ConfigReader.GetOrDefault("PASSWORD", "secret_sauce"));
    }

    [When("I click the login button")]
    public void WhenIClickTheLoginButton()
    {
        _loginPage.Submit();
    }

    [Then("I should be redirected to the homepage")]
    public void ThenIShouldBeRedirectedToTheHomepage()
    {
        Assert.That(
            _loginPage.IsLoaded(),
            Is.True,
            $"Expected the application page to load after login, but it did not. URL: {_driver.Url}");
    }
}
