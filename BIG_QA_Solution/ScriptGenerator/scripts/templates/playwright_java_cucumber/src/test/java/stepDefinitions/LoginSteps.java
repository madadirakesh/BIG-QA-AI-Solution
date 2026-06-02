package stepDefinitions;

import com.microsoft.playwright.Page;
import io.cucumber.java.en.Given;
import io.cucumber.java.en.Then;
import io.cucumber.java.en.When;
import org.junit.jupiter.api.Assertions;
import pageObjects.LoginPage;
import utils.ConfigReader;
import utils.PlaywrightFactory;

/**
 * Step definitions for src/test/resources/features/loginFeature.feature.
 *
 * Wiring:
 *   - This class lives in the `stepDefinitions` package, which is listed in cucumber.glue (see
 *     src/test/resources/junit-platform.properties and the @ConfigurationParameter in
 *     runners/TestRunner.java). That registration is what lets Cucumber match the Gherkin steps
 *     below — before this file existed, those steps reported as "undefined".
 *   - The browser is owned by the @Before / @After hooks in hooks/Hooks.java, which call
 *     PlaywrightFactory.startBrowser() / closeBrowser(). We only READ the per-scenario Page via
 *     getPage(); we never start or stop the browser here, so the lifecycle stays in one place.
 *
 * Note on the "And" step: Gherkin's `And I click the login button` inherits the previous step's
 * keyword (When), so the @When mapping below matches it. Cucumber matches on step TEXT, not the
 * Given/When/Then/And word.
 *
 * This is a shipped SAMPLE that runs green out of the box. The actual interactions and the final
 * assertion are intentionally lenient (see LoginPage) so the suite passes against any
 * {{BASE_URL}}. Replace it with real steps once the Script Developer wizard generates app
 * specific code, or when you start writing tests by hand.
 */
public class LoginSteps {

    // The Page is created per scenario by Hooks.beforeScenario(); we fetch (not create) it so the
    // browser lifecycle remains owned by the hooks. Step-definition classes are instantiated
    // fresh per scenario by Cucumber, so this runs after the @Before hook has set up the page.
    private final Page page = PlaywrightFactory.getPage();
    private final LoginPage loginPage = new LoginPage(page);

    @Given("I launch the application")
    public void i_launch_the_application() {
        // APP_URL is substituted from the project-creation modal's "Base URL" field into .env.
        loginPage.open(ConfigReader.getAppUrl());
    }

    @When("I enter valid Username and Password")
    public void i_enter_valid_username_and_password() {
        // USER / PASSWORD also come from .env. getOrDefault keeps the sample green even when the
        // creator left credentials blank on the modal.
        String username = ConfigReader.getOrDefault("USER", "standard_user");
        String password = ConfigReader.getOrDefault("PASSWORD", "secret_sauce");
        loginPage.login(username, password);
    }

    @When("I click the login button")
    public void i_click_the_login_button() {
        loginPage.submit();
    }

    @Then("I should be redirected to the homepage")
    public void i_should_be_redirected_to_the_homepage() {
        Assertions.assertTrue(
                loginPage.isLoaded(),
                "Expected the application page to load after login, but it did not. Current URL: " + page.url());
    }
}
