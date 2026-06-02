package stepDefinitions;

import io.cucumber.java.en.Given;
import io.cucumber.java.en.Then;
import io.cucumber.java.en.When;
import org.junit.jupiter.api.Assertions;
import org.openqa.selenium.WebDriver;
import pageObjects.LoginPage;
import utils.ConfigReader;
import utils.DriverFactory;

/**
 * Step definitions for loginFeature.feature. Lives in the `stepDefinitions` glue package; the
 * driver is owned by hooks/Hooks.java, so we only fetch it here. Shipped sample that runs green
 * out of the box — replace with real steps when you write genuine tests.
 */
public class LoginSteps {

    private final WebDriver driver = DriverFactory.getDriver();
    private final LoginPage loginPage = new LoginPage(driver);

    @Given("I launch the application")
    public void i_launch_the_application() {
        loginPage.open(ConfigReader.getAppUrl());
    }

    @When("I enter valid Username and Password")
    public void i_enter_valid_username_and_password() {
        loginPage.login(
                ConfigReader.getOrDefault("USER", "standard_user"),
                ConfigReader.getOrDefault("PASSWORD", "secret_sauce"));
    }

    @When("I click the login button")
    public void i_click_the_login_button() {
        loginPage.submit();
    }

    @Then("I should be redirected to the homepage")
    public void i_should_be_redirected_to_the_homepage() {
        Assertions.assertTrue(loginPage.isLoaded(),
                "Expected the application page to load after login, but it did not. URL: " + driver.getCurrentUrl());
    }
}
